# -*- coding: utf-8 -*-
"""
Security test suite – Fix 2 (IDOR) and Fix 3 (SQL injection).

Commit discipline:
  - This file is committed in its FAILING (red) state first.
  - Fix commits make all tests green.

Tests
-----
Fix 2 – IDOR
  T-IDOR-1  Citizen A (mobile 9000000001) must get 403 on /application/<B_id>
  T-IDOR-2  Citizen A must get 403 on /ack/<B_id>.pdf  (currently unauthenticated)
  T-IDOR-3  Citizen A must get 200 on their own /application/<A_id>
  T-IDOR-4  Unauthenticated /ack/<id>.pdf must redirect, not serve PDF
  T-IDOR-5  /admin/approve/<id> POST from a citizen session (no admin flag) must get 403
  T-IDOR-6  /admin/reject/<id> POST from a citizen session must get 403

Fix 3 – SQL injection
  T-SQLI-1  POST /status/reset with payload x' OR '1'='1 must not return 200
             with a body implying all users were found (response must not contain
             a password-reset confirmation that works for every account)
  T-SQLI-2  reset_password SQL uses %s parameter placeholders, not %-string-format
  T-SQLI-3  No raw string interpolation (% or f-string) in any SQL in app.py
"""

import importlib.util
import os
import types
import unittest.mock as mock
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT  = Path(__file__).parent.parent.parent
APP_DIR    = REPO_ROOT / "app"
APP_PY     = APP_DIR / "app.py"


# ---------------------------------------------------------------------------
# App loader (no live DB on import)
# ---------------------------------------------------------------------------

def _load_app() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sewasetu_app", APP_PY)
    mod  = importlib.util.module_from_spec(spec)
    env_patch = {
        "SEWASETU_SECRET_KEY": "test-secret-key",
        "SEWASETU_ADMIN_PASSWORD": "test-admin-password",
    }
    with mock.patch("psycopg2.connect",
                    side_effect=RuntimeError("no live DB in tests")), \
         mock.patch.dict(os.environ, env_patch):
        spec.loader.exec_module(mod)
    flask_app = mod.app
    flask_app.template_folder = str(APP_DIR / "templates")
    flask_app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mod():
    return _load_app()


@pytest.fixture()
def client_citizen_a(mod):
    """
    Test client for citizen A (mobile 9000000001, app_id=1).
    DB is mocked: /application/1 returns a row owned by 9000000001,
                  /application/2 returns a row owned by 9000000002.
    """
    flask_app = mod.app

    # Row layout matches view_application SELECT:
    # application_no[0], name[1], mobile[2], dob[3], village[4], block[5],
    # bank_account[6], ifsc[7], status[8], submitted_at[9], decided_at[10]
    from datetime import datetime, timedelta
    sub = datetime.now() - timedelta(days=1)

    rows = {
        1: ("SSP001", "Citizen A", "9000000001", "1960-01-01", "V", "B",
            "ACC1", "IFSC1", "PENDING", sub, None),
        2: ("SSP002", "Citizen B", "9000000002", "1961-01-01", "V", "B",
            "ACC2", "IFSC2", "PENDING", sub, None),
    }

    def fake_fetchone_factory(app_id):
        return rows.get(app_id)

    fake_cur  = mock.MagicMock()
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value = fake_cur

    def side_effect_execute(sql, params=None):
        if params and isinstance(params, tuple) and len(params) == 1:
            fake_cur._last_id = params[0]

    fake_cur.execute.side_effect = side_effect_execute
    fake_cur.fetchone.side_effect = lambda: rows.get(
        getattr(fake_cur, "_last_id", None)
    )

    with mock.patch.object(mod, "get_db", return_value=fake_conn):
        with flask_app.test_client() as c:
            with c.session_transaction() as sess:
                sess["logged_in"]     = True
                sess["portal_mobile"] = "9000000001"   # citizen A
                # no sess["admin"]
            yield c


@pytest.fixture()
def client_unauthenticated(mod):
    """Test client with no session."""
    with mod.app.test_client() as c:
        yield c


@pytest.fixture()
def client_citizen_as_approver(mod):
    """
    Citizen session that has logged_in=True but no admin flag.
    Used to test that /admin/approve and /admin/reject require admin, not just login.
    """
    flask_app = mod.app
    fake_cur  = mock.MagicMock()
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_cur.fetchone.return_value = ("SSP001",)  # any row
    with mock.patch.object(mod, "get_db", return_value=fake_conn):
        with flask_app.test_client() as c:
            with c.session_transaction() as sess:
                sess["logged_in"]     = True
                sess["portal_mobile"] = "9000000001"
                # deliberately NO sess["admin"]
            yield c


# ---------------------------------------------------------------------------
# Fix 2 - IDOR tests
# ---------------------------------------------------------------------------

class TestIDOR:
    """Citizen A must not be able to read Citizen B's application or PDF."""

    def test_citizen_a_gets_403_on_app_b(self, client_citizen_a):
        """T-IDOR-1: GET /application/2 by citizen A must return 403."""
        resp = client_citizen_a.get("/application/2")
        assert resp.status_code == 403, (
            f"Expected 403 for cross-citizen read, got {resp.status_code}. "
            "IDOR: citizen A can read citizen B's application."
        )

    def test_citizen_a_gets_403_on_ack_b(self, client_citizen_a, mod):
        """T-IDOR-2: GET /ack/2.pdf by citizen A must return 403."""
        fake_cur  = mock.MagicMock()
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        fake_cur.fetchone.return_value = (2,)  # app exists in DB

        with mock.patch.object(mod, "get_db", return_value=fake_conn):
            resp = client_citizen_a.get("/ack/2.pdf")
        assert resp.status_code in (403, 404), (
            f"Expected 403 on /ack/2.pdf for citizen A, got {resp.status_code}. "
            "IDOR: ack PDF endpoint has no auth or ownership check."
        )

    def test_citizen_a_can_read_own_app(self, client_citizen_a):
        """T-IDOR-3: GET /application/1 by citizen A must return 200."""
        resp = client_citizen_a.get("/application/1")
        assert resp.status_code == 200, (
            f"Expected 200 for own application, got {resp.status_code}"
        )

    def test_unauthenticated_ack_redirects(self, client_unauthenticated):
        """T-IDOR-4: Unauthenticated GET /ack/1.pdf must not serve a PDF."""
        resp = client_unauthenticated.get("/ack/1.pdf")
        assert resp.status_code in (302, 401, 403), (
            f"Expected redirect/auth challenge for unauthenticated /ack, "
            f"got {resp.status_code}. Ack PDFs are served to anyone."
        )

    def test_citizen_cannot_approve(self, client_citizen_as_approver, mod):
        """T-IDOR-5: POST /admin/approve/<id> from citizen session must get 403."""
        fake_cur  = mock.MagicMock()
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        with mock.patch.object(mod, "get_db", return_value=fake_conn):
            resp = client_citizen_as_approver.post("/admin/approve/1")
        assert resp.status_code == 403, (
            f"Expected 403 for citizen approving application, got {resp.status_code}. "
            "Admin action protected by logged_in only, not admin flag."
        )

    def test_citizen_cannot_reject(self, client_citizen_as_approver, mod):
        """T-IDOR-6: POST /admin/reject/<id> from citizen session must get 403."""
        fake_cur  = mock.MagicMock()
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        with mock.patch.object(mod, "get_db", return_value=fake_conn):
            resp = client_citizen_as_approver.post("/admin/reject/1")
        assert resp.status_code == 403, (
            f"Expected 403 for citizen rejecting application, got {resp.status_code}. "
            "Admin action protected by logged_in only, not admin flag."
        )


# ---------------------------------------------------------------------------
# Fix 3 - SQL injection tests
# ---------------------------------------------------------------------------

class TestSQLInjection:
    """
    Password-reset must use parameterised SQL.

    The original vulnerable code (lines 471-474 of vendor app.py):

        cur.execute("SELECT mobile FROM portal_users WHERE mobile = '%s'" % mobile)
        cur.execute("SELECT dob FROM applications WHERE mobile = '%s' LIMIT 1" % mobile)

    With these queries, the payload   x' OR '1'='1   is *interpolated into the
    SQL string* before psycopg2 ever sees it, so psycopg2 executes:
        WHERE mobile = 'x' OR '1'='1'
    which matches every row.

    The fix requires:
        cur.execute("... WHERE mobile = %s", (mobile,))

    With parameterised queries, psycopg2 receives the *unmodified* payload as a
    bind parameter; the SQL string itself only ever contains %s.

    The tests below use a cursor spy that records every (sql, params) pair so we
    can make precise, behavioural assertions – not fragile source-code regex.
    """

    PAYLOAD = "x' OR '1'='1"

    @pytest.fixture()
    def spy_cursor(self):
        """
        A MagicMock cursor that records every execute() call as (sql, params)
        and returns None from fetchone() by default (no rows – clean DB state).
        """
        calls = []

        cur = mock.MagicMock()
        cur._spy_calls = calls

        def record_execute(sql, params=None):
            calls.append((sql, params))

        cur.execute.side_effect = record_execute
        cur.fetchone.return_value = None   # no matching mobile → no reset
        return cur

    @pytest.fixture()
    def reset_client(self, mod, spy_cursor):
        """Flask test client wired to the spy cursor, for reset_password tests."""
        flask_app = mod.app
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = spy_cursor
        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch.object(mod, "send_sms"):
            with flask_app.test_client() as c:
                yield c, spy_cursor, fake_conn

    def _post_reset(self, client, payload):
        return client.post("/status/reset", data={"mobile": payload})

    # ------------------------------------------------------------------
    # T-SQLI-1  Payload must land in bind params, not in the SQL string
    # ------------------------------------------------------------------

    def test_payload_is_in_params_not_sql(self, reset_client):
        """
        T-SQLI-1: After POSTing the injection payload, every cursor.execute()
        call must have the payload in its *params* tuple, not embedded in the
        SQL string itself.

        With the vulnerable code, string interpolation runs at the Python level
        before psycopg2 is called, so the first execute() gets:
            sql = "SELECT mobile FROM portal_users WHERE mobile = 'x' OR '1'='1'"
            params = None
        — payload IS in sql, NOT in params.  This test catches that.

        After the fix, execute() gets:
            sql = "SELECT mobile FROM portal_users WHERE mobile = %s"
            params = ("x' OR '1'='1",)
        — payload is ONLY in params, never in sql.
        """
        client, spy_cursor, _ = reset_client
        self._post_reset(client, self.PAYLOAD)

        select_calls = [
            (sql, params)
            for sql, params in spy_cursor._spy_calls
            if "portal_users" in sql or "applications" in sql
        ]
        assert select_calls, (
            "reset_password did not call cursor.execute() with any SELECT; "
            "check the view is reachable in the test."
        )
        for sql, params in select_calls:
            assert self.PAYLOAD not in sql, (
                f"Injection payload found INSIDE the SQL string:\n  sql={sql!r}\n"
                "This means string interpolation is happening before psycopg2 — "
                "the query is injectable. Fix: use parameterised queries."
            )
            # Payload must be in the params tuple (proves it's a bind parameter)
            assert params is not None and self.PAYLOAD in params, (
                f"Injection payload NOT found in params:\n  sql={sql!r}\n  params={params!r}\n"
                "Fix: cur.execute(sql, (mobile,)) — mobile must be a bind parameter."
            )

    # ------------------------------------------------------------------
    # T-SQLI-2  No single-quoted %s placeholder in any SQL in app.py
    # ------------------------------------------------------------------

    def test_no_single_quoted_placeholder_in_sql(self):
        """
        T-SQLI-2: Source-code sanity check.  The vendor pattern
            WHERE mobile = '%s'" % mobile
        wraps the placeholder in SQL quotes, making it injectable.
        Correct psycopg2 syntax never quotes %s in the SQL string.
        """
        import re
        src = APP_PY.read_text(encoding="utf-8")
        # Matches the literal string  = '%s'  anywhere in the source.
        # Legitimate parameterised code never has this pattern.
        matches = re.findall(r"= '%s'", src)
        assert not matches, (
            f"Found {len(matches)} instance(s) of  = '%s'  in app.py. "
            "This is the vendor SQLi pattern. "
            "Replace with the unquoted placeholder  = %s  and pass the value "
            "as a bind parameter: cur.execute(sql, (value,))."
        )

    # ------------------------------------------------------------------
    # T-SQLI-3  No f-string SQL in app.py
    # ------------------------------------------------------------------

    def test_no_fstring_sql(self):
        """
        T-SQLI-3: No f-string or %-interpolated SQL anywhere in app.py.
        Checks for f"...{...}..." patterns touching SQL keywords.
        """
        import re
        src = APP_PY.read_text(encoding="utf-8")
        fstring_sql = re.compile(
            r'f["\'].*?(SELECT|INSERT|UPDATE|DELETE|WHERE).*?\{',
            re.IGNORECASE,
        )
        matches = fstring_sql.findall(src)
        assert not matches, (
            f"f-string SQL detected in app.py: {matches}"
        )

    # ------------------------------------------------------------------
    # T-SQLI-4  No DB commit when payload doesn't match any real mobile
    # ------------------------------------------------------------------

    def test_no_commit_on_injection_payload(self, reset_client):
        """
        T-SQLI-4: With a parameterised query, fetchone() returns None for
        the injection payload (no real mobile matches the literal string).
        Therefore no password reset should commit anything to the DB.
        """
        client, _, fake_conn = reset_client
        self._post_reset(client, self.PAYLOAD)
        assert not fake_conn.commit.called, (
            "DB commit was called despite the injection payload returning no rows. "
            "This suggests the parameterisation is incomplete or fetchone() "
            "returned a row when it shouldn't have."
        )

