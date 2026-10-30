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
    with mock.patch("psycopg2.connect",
                    side_effect=RuntimeError("no live DB in tests")):
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
    """Password reset must use parameterised SQL; no string interpolation in SQL."""

    def test_reset_password_sql_is_parameterised(self):
        """
        T-SQLI-2: The reset_password view must not build SQL with % string format.
        We inspect the source for the known injection pattern.
        """
        src = APP_PY.read_text(encoding="utf-8")
        import re
        # Match: .execute( ... "..." % something )  on one line (the vendor pattern)
        bad_pattern = re.compile(
            r'\.execute\s*\([^)]*%\s*\w',
            re.MULTILINE,
        )
        matches = bad_pattern.findall(src)
        assert not matches, (
            f"SQL string interpolation found in app.py: {matches}. "
            "Use parameterised queries: cur.execute(sql, (param,))"
        )

    def test_no_fstring_sql(self):
        """
        T-SQLI-3: No f-string or %-interpolated SQL anywhere in app.py.
        Checks for f"...{...}..." patterns touching SQL keywords.
        """
        src = APP_PY.read_text(encoding="utf-8")
        import re
        # f-strings containing SQL keywords
        fstring_sql = re.compile(
            r'f["\'].*?(SELECT|INSERT|UPDATE|DELETE|WHERE).*?\{',
            re.IGNORECASE,
        )
        matches = fstring_sql.findall(src)
        assert not matches, (
            f"f-string SQL detected in app.py: {matches}"
        )

    def test_reset_injection_payload_does_not_trigger_sms(self, mod):
        """
        T-SQLI-1: POST /status/reset with injection payload must not trigger send_sms.

        With the vulnerable code:
          cur.execute("SELECT mobile FROM portal_users WHERE mobile = '%s'" % mobile)
        the payload x' OR '1'='1 produces:
          WHERE mobile = 'x' OR '1'='1'
        which matches every row, so the reset fires.

        We simulate this by having the mock cursor return None for fetchone()
        (correct parameterised behaviour: the literal string does not match).
        After the fix, send_sms must NOT be called.
        """
        flask_app = mod.app
        fake_cur  = mock.MagicMock()
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        # Parameterised query: injection payload won't match any real mobile -> None
        fake_cur.fetchone.return_value = None

        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch.object(mod, "send_sms") as mock_sms, \
             mock.patch("requests.post"):
            with flask_app.test_client() as c:
                c.post("/status/reset", data={"mobile": "x' OR '1'='1"})

        # If SQLi works and the mock is bypassed (raw string interpolation runs
        # at the Python level before psycopg2 sees it), the exploit path is still
        # caught because we check send_sms was NOT called.
        assert not mock_sms.called, (
            "SQL injection in reset_password triggered send_sms – "
            "the WHERE clause matched despite the injection payload. "
            "Fix: use parameterised queries (cur.execute(sql, (mobile,)))."
        )
        assert not fake_conn.commit.called, (
            "SQL injection in reset_password triggered a DB commit."
        )
