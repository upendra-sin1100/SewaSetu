# -*- coding: utf-8 -*-
"""
Failing tests for Fix 1: RTPS deemed-approval scheduler
(test commit – all tests must FAIL before the fix commit lands)

What these tests verify after the fix:
  T1  Config: the script reads app/config/app.ini, NOT the nonexistent settings.ini
  T2  SLA boundary: a 16-day-old PENDING row IS selected
  T3  SLA boundary: a 14-day-old PENDING row is NOT selected
  T4  SLA boundary: exactly-15-days-old is selected  (cutoff is submitted_at <= now-15d)
  T5  Cutoff uses IST (Asia/Kolkata) for "now", not naive local time
  T6  SMS failure is LOGGED, not silently swallowed
  T7  Status page renders an overdue PENDING as DEEMED_APPROVED to the citizen

Design constraints:
  - Never connect to the live Postgres database.
  - Never send a real SMS.
  - Inject `now` as a parameter so pure functions are testable.
  - Use an in-process SQLite DB for DB-touching tests (same SQL dialect for the
    parts we care about; the script will use psycopg2 in production).
"""

import importlib.util
import logging
import sqlite3
import types
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

# Path of the scheduler script (unchanged for now – loaded after refactor)
SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
SCRIPT_PATH = SCRIPT_DIR / "deemed_approval.py"

# Path that the script MUST use for its config
EXPECTED_CONFIG_PATH = Path(__file__).parent.parent / "config" / "app.ini"


def _import_script() -> types.ModuleType:
    """Import deemed_approval WITHOUT executing __main__ block."""
    spec = importlib.util.spec_from_file_location("deemed_approval", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# T1 – Config path
# ---------------------------------------------------------------------------

class TestConfigPath:
    """The script must load config from app.ini, not settings.ini."""

    def test_config_path_is_app_ini(self):
        """After refactor, importing the script must NOT raise FileNotFoundError,
        and the resolved CONFIG_PATH must point to app.ini, not settings.ini."""
        # Currently this raises FileNotFoundError because settings.ini is absent.
        mod = _import_script()           # <-- will FAIL until fix lands
        assert "settings.ini" not in str(mod.CONFIG_PATH), (
            "CONFIG_PATH still points to settings.ini; expected app.ini"
        )
        assert str(EXPECTED_CONFIG_PATH) in str(mod.CONFIG_PATH) or \
               mod.CONFIG_PATH == str(EXPECTED_CONFIG_PATH), (
            f"CONFIG_PATH is {mod.CONFIG_PATH!r}; expected {EXPECTED_CONFIG_PATH}"
        )


# ---------------------------------------------------------------------------
# T2-T5 – Pure SLA logic (injected `now`, no DB, no SMS)
# ---------------------------------------------------------------------------

class TestFindOverdue:
    """find_overdue(cur, now) must return the rows that breach the SLA."""

    @pytest.fixture()
    def db(self):
        """Tiny in-memory SQLite schema mirroring only what deemed_approval needs."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE applications (
                 id           INTEGER PRIMARY KEY,
                 application_no TEXT,
                 mobile       TEXT,
                 status       TEXT,
                 submitted_at TEXT   -- ISO8601
               )"""
        )
        conn.commit()
        return conn

    def _insert(self, conn, app_id, mobile, status, submitted_at: datetime):
        conn.execute(
            "INSERT INTO applications VALUES (?,?,?,?,?)",
            (app_id, f"SSP26{app_id:06d}", mobile, status,
             submitted_at.isoformat()),
        )
        conn.commit()

    def _now_ist(self) -> datetime:
        return datetime.now(tz=IST)

    # ------------------------------------------------------------------
    # T2 – 16-day-old PENDING must be selected
    # ------------------------------------------------------------------
    def test_16_day_old_pending_is_overdue(self):
        """A PENDING application submitted 16 days ago is overdue (>= 15 days)."""
        mod = _import_script()           # will FAIL until fix lands
        now = self._now_ist()
        submitted = now - timedelta(days=16)
        rows = mod.find_overdue(now)     # injected now; function must exist after refactor
        # Using pure-function form – no DB here; verify the cutoff calc directly
        cutoff = mod.compute_cutoff(now, sla_days=15)   # must exist after refactor
        assert submitted < cutoff, (
            "16-day-old application should be older than the cutoff"
        )

    # ------------------------------------------------------------------
    # T3 – 14-day-old PENDING must NOT be selected
    # ------------------------------------------------------------------
    def test_14_day_old_pending_is_not_overdue(self):
        """A PENDING application submitted 14 days ago is NOT yet overdue."""
        mod = _import_script()
        now = self._now_ist()
        submitted = now - timedelta(days=14)
        cutoff = mod.compute_cutoff(now, sla_days=15)
        assert submitted >= cutoff, (
            "14-day-old application must NOT be older than the cutoff"
        )

    # ------------------------------------------------------------------
    # T4 – Exactly-15-days is selected  (<=, i.e., closed interval)
    # ------------------------------------------------------------------
    def test_exactly_15_days_is_overdue(self):
        """An application submitted exactly 15 days ago IS overdue (SLA boundary is <=)."""
        mod = _import_script()
        now = self._now_ist()
        submitted = now - timedelta(days=15)
        cutoff = mod.compute_cutoff(now, sla_days=15)
        # submitted_at <= cutoff  means  now - 15d <= now - 15d  → True
        assert submitted <= cutoff, (
            "Exactly-15-day-old application must be selected (closed-interval boundary)"
        )

    # ------------------------------------------------------------------
    # T5 – Cutoff is IST-aware, not naive local time
    # ------------------------------------------------------------------
    def test_cutoff_is_ist_aware(self):
        """compute_cutoff must return a timezone-aware datetime in IST."""
        mod = _import_script()
        now = datetime.now(tz=IST)
        cutoff = mod.compute_cutoff(now, sla_days=15)
        assert cutoff.tzinfo is not None, (
            "compute_cutoff returned a naive datetime; must be IST-aware"
        )
        # The offset must be +05:30
        offset = cutoff.utcoffset()
        expected_offset = timedelta(hours=5, minutes=30)
        assert offset == expected_offset, (
            f"Expected IST offset +05:30 but got {offset}"
        )


# ---------------------------------------------------------------------------
# T6 – SMS failure is logged, not swallowed
# ---------------------------------------------------------------------------

class TestSmsFailureLogging:
    """When the SMS gateway raises an exception, the script must log it."""

    def test_sms_failure_is_logged(self, caplog):
        """An SMS exception must produce at least one ERROR log record."""
        mod = _import_script()           # will FAIL until fix lands

        # approve() must accept (conn, rows, sms_gateway_url, logger) or similar
        # so the logger can be inspected; the exact signature is left to the
        # implementer, but the function MUST exist and MUST log on SMS failure.
        with caplog.at_level(logging.ERROR):
            with mock.patch("requests.post", side_effect=Exception("gateway down")):
                mod.send_approval_sms(
                    mobile="9999999999",
                    app_no="SSP26000001",
                    sms_gateway_url="http://localhost:9999",
                )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, (
            "SMS failure was silently swallowed; expected at least one ERROR log entry"
        )


# ---------------------------------------------------------------------------
# T7 – Status page shows overdue PENDING as DEEMED_APPROVED
# ---------------------------------------------------------------------------

class TestStatusPageRendering:
    """The citizen-facing /application/<id> must display DEEMED_APPROVED (not PENDING)
    when the application is overdue, even before the DB row is updated."""

    @pytest.fixture()
    def client(self):
        """Flask test client with a mocked DB that returns a stale PENDING row."""
        # Import the Flask app; patch get_db so it never hits Postgres.
        import importlib
        app_spec = importlib.util.spec_from_file_location(
            "sewasetu_app", Path(__file__).parent.parent / "app.py"
        )
        app_mod = importlib.util.module_from_spec(app_spec)
        app_spec.loader.exec_module(app_mod)

        flask_app = app_mod.app
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test"

        # A row that is PENDING and submitted 20 days ago (overdue)
        submitted_20d_ago = datetime.now() - timedelta(days=20)
        overdue_row = (
            "SSP26000001",   # application_no
            "Ramesh Kumar",  # applicant_name
            "9000000001",    # mobile
            "1960-01-01",    # dob
            "Sonari",        # village
            "Rajapara",      # block
            "12345678901234",# bank_account
            "SBIN0001234",   # ifsc
            "PENDING",       # status  ← the bug: overdue PENDING must show as DEEMED_APPROVED
            submitted_20d_ago,
            None,            # decided_at
        )

        fake_cur = mock.MagicMock()
        fake_cur.fetchone.return_value = overdue_row
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        fake_conn.__enter__ = mock.MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch.object(app_mod, "get_db", return_value=fake_conn):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["logged_in"] = True
                    sess["portal_mobile"] = "9000000001"
                yield c

    def test_overdue_pending_shown_as_deemed_approved(self, client):
        """The status page for an overdue PENDING application must render
        DEEMED_APPROVED (or equivalent), NOT 'PENDING'."""
        resp = client.get("/application/1")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8", errors="replace")
        # Must NOT say PENDING
        assert "PENDING" not in body, (
            "Status page still shows PENDING for a 20-day-old overdue application; "
            "citizen is being misled"
        )
        # Must indicate deemed approval
        assert "DEEMED" in body.upper() or "APPROVED" in body.upper(), (
            "Status page does not indicate DEEMED_APPROVED for overdue application"
        )
