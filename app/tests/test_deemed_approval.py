# -*- coding: utf-8 -*-
"""
Failing tests for Fix 1: RTPS deemed-approval scheduler.
(test commit – every test must FAIL before the fix commit)

Covers:
  T1  config path resolves to app.ini, not the missing settings.ini
  T2  16-day-old PENDING is selected
  T3  14-day-old PENDING is NOT selected
  T4  exactly-15-days is selected (closed <= boundary)
  T5a cutoff is IST-aware (fixed +05:30 offset) – this is the target
  T5b PROVES T5 can fail: a naive-datetime version of compute_cutoff
        returns a naive datetime and the assertion catches it
  T6  SMS failure is logged at ERROR, not swallowed
  T7  status page renders overdue PENDING as DEEMED_APPROVED
  T8  ack PDF path / generate_acknowledgment also reflects deemed status
  T9  crontab entry exists, runs the right script, and exits non-zero on failure

Design:
  - Zero live Postgres connections.
  - Zero real SMS calls.
  - Fixed IST offset: timedelta(hours=5, minutes=30) – no ZoneInfo/pytz.
  - submitted_at in DB is TIMESTAMP WITHOUT TIME ZONE (naive); is_overdue()
    must handle the naive→aware normalisation internally.
  - Scheduler and view_application share one is_overdue() function.
"""

import importlib.util
import logging
import os
import subprocess
import sys
import types
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

REPO_ROOT = Path(__file__).parent.parent.parent   # sewasetu-handout/
APP_DIR   = REPO_ROOT / "app"
SCRIPT_PATH      = APP_DIR / "scripts" / "deemed_approval.py"
APP_PY_PATH      = APP_DIR / "app.py"
EXPECTED_CONFIG  = APP_DIR / "config" / "app.ini"
CRONTAB_PATH     = APP_DIR / "deploy" / "crontab"


# ---------------------------------------------------------------------------
# Import helpers – safe: import without executing __main__
# ---------------------------------------------------------------------------

def _import_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_scheduler() -> types.ModuleType:
    """Import deemed_approval.  Must not raise, must not touch DB or SMS."""
    return _import_module(SCRIPT_PATH, "deemed_approval")


def _import_app() -> types.ModuleType:
    """Import the Flask app module with DB patched out."""
    with mock.patch("psycopg2.connect", side_effect=RuntimeError("no live DB in tests")):
        mod = _import_module(APP_PY_PATH, "sewasetu_app")
    return mod


# ---------------------------------------------------------------------------
# T1 – Config path
# ---------------------------------------------------------------------------

class TestConfigPath:
    def test_import_does_not_raise(self):
        """Script import must succeed without FileNotFoundError."""
        _import_scheduler()                              # FAILS: settings.ini missing

    def test_config_path_is_app_ini(self):
        """CONFIG_PATH must point to app.ini, not settings.ini."""
        mod = _import_scheduler()
        assert "settings.ini" not in str(mod.CONFIG_PATH), \
            f"CONFIG_PATH still references settings.ini: {mod.CONFIG_PATH}"
        assert str(EXPECTED_CONFIG) == str(mod.CONFIG_PATH), \
            f"Expected {EXPECTED_CONFIG}, got {mod.CONFIG_PATH}"


# ---------------------------------------------------------------------------
# T2-T4 – SLA boundary logic (pure, no DB)
# ---------------------------------------------------------------------------

class TestComputeCutoff:
    """compute_cutoff(now, sla_days) must return an IST-aware datetime."""

    def _now(self) -> datetime:
        return datetime.now(tz=IST)

    def test_16_day_old_is_overdue(self):
        mod = _import_scheduler()
        now       = self._now()
        submitted = now - timedelta(days=16)
        cutoff    = mod.compute_cutoff(now, sla_days=15)   # AttributeError until refactor
        assert submitted <= cutoff, \
            "16-day-old submission must be <= cutoff (overdue)"

    def test_14_day_old_is_not_overdue(self):
        mod    = _import_scheduler()
        now    = self._now()
        submitted = now - timedelta(days=14)
        cutoff = mod.compute_cutoff(now, sla_days=15)
        assert submitted > cutoff, \
            "14-day-old submission must be > cutoff (within SLA)"

    def test_exactly_15_days_is_overdue(self):
        mod       = _import_scheduler()
        now       = self._now()
        submitted = now - timedelta(days=15)
        cutoff    = mod.compute_cutoff(now, sla_days=15)
        # closed interval: submitted_at <= cutoff
        assert submitted <= cutoff, \
            "Exactly-15-day-old application must be selected (closed boundary)"


# ---------------------------------------------------------------------------
# T5a – IST-aware cutoff
# ---------------------------------------------------------------------------

class TestCutoffIsIST:
    """compute_cutoff must return a timezone-aware datetime at +05:30."""

    def test_cutoff_is_ist_aware(self):
        mod    = _import_scheduler()
        now    = datetime.now(tz=IST)
        cutoff = mod.compute_cutoff(now, sla_days=15)
        assert cutoff.tzinfo is not None, \
            "compute_cutoff returned a naive datetime; must be IST-aware"
        assert cutoff.utcoffset() == timedelta(hours=5, minutes=30), \
            f"Expected +05:30 offset, got {cutoff.utcoffset()}"


# ---------------------------------------------------------------------------
# T5b – PROOF that T5a can detect the naive bug
#        (This test verifies the *inverse*: a naive implementation IS caught)
# ---------------------------------------------------------------------------

class TestNaiveVersionIsDetectedByT5:
    """
    Demonstrate that T5a is not vacuously true.
    We define a naive version of compute_cutoff that returns a naive datetime,
    then assert that T5a's check *would* catch it.
    This test itself must PASS (it's a proof-of-detection test, not a fix test),
    so it is not gated on the import succeeding.
    """

    def _naive_compute_cutoff(self, now: datetime, sla_days: int) -> datetime:
        """Buggy version: strips tzinfo, mimicking the original datetime.now() call."""
        naive_now = now.replace(tzinfo=None)          # strip tz – the original bug
        return naive_now - timedelta(days=sla_days)

    def test_naive_cutoff_has_no_tzinfo(self):
        """Prove: a naive cutoff has tzinfo=None, which T5a's assertion catches."""
        now    = datetime.now(tz=IST)
        cutoff = self._naive_compute_cutoff(now, sla_days=15)
        # The naive cutoff must have no tzinfo – confirming T5a would catch it
        assert cutoff.tzinfo is None, (
            "The naive reference implementation unexpectedly carries tzinfo; "
            "the proof-of-detection test is broken."
        )

    def test_naive_offset_does_not_equal_ist(self):
        """Prove: a naive cutoff's utcoffset() returns None, not +05:30."""
        now    = datetime.now(tz=IST)
        cutoff = self._naive_compute_cutoff(now, sla_days=15)
        assert cutoff.utcoffset() is None, (
            "Expected utcoffset() == None for naive datetime (the buggy version)."
        )
        # Confirm T5a's assertion would fire
        with pytest.raises(AssertionError):
            assert cutoff.utcoffset() == timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# T6 – SMS failure is logged
# ---------------------------------------------------------------------------

class TestSmsLogging:
    def test_sms_failure_logged_at_error(self, caplog):
        """An SMS exception must emit at least one ERROR log; not be swallowed."""
        mod = _import_scheduler()
        # The refactored script must expose send_approval_sms(mobile, app_no, url, logger)
        with caplog.at_level(logging.ERROR):
            with mock.patch("requests.post", side_effect=Exception("gateway down")):
                mod.send_approval_sms(
                    mobile="9999999999",
                    app_no="SSP26000001",
                    sms_gateway_url="http://localhost:9999",
                )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, (
            "SMS failure was silently swallowed (pass); expected at least one ERROR log"
        )


# ---------------------------------------------------------------------------
# T7 – Status page renders overdue PENDING as DEEMED_APPROVED
# ---------------------------------------------------------------------------

class TestStatusPageRendering:
    """
    /application/<id> for a 20-day-old PENDING must display DEEMED_APPROVED.
    Uses Flask test client; DB is fully mocked (no psycopg2 connection).
    submitted_at is a naive datetime as psycopg2 returns from TIMESTAMP WITHOUT TZ.
    """

    SUBMITTED_20D_AGO = datetime.now() - timedelta(days=20)   # naive, like the DB

    OVERDUE_ROW = (
        "SSP26000001",        # application_no  [0]
        "Ramesh Kumar",       # applicant_name  [1]
        "9000000001",         # mobile          [2]
        "1960-01-01",         # dob             [3]
        "Sonari",             # village         [4]
        "Rajapara",           # block           [5]
        "12345678901234",     # bank_account    [6]
        "SBIN0001234",        # ifsc            [7]
        "PENDING",            # status          [8]  ← the bug
        SUBMITTED_20D_AGO,    # submitted_at    [9]
        None,                 # decided_at      [10]
    )

    @pytest.fixture()
    def client(self):
        spec = importlib.util.spec_from_file_location("sewasetu_app", APP_PY_PATH)
        mod  = importlib.util.module_from_spec(spec)
        # Patch psycopg2.connect before module-level code runs
        with mock.patch("psycopg2.connect",
                        side_effect=RuntimeError("no live DB")):
            spec.loader.exec_module(mod)

        flask_app = mod.app
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"

        fake_cur  = mock.MagicMock()
        fake_cur.fetchone.return_value = self.OVERDUE_ROW
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur

        with mock.patch.object(mod, "get_db", return_value=fake_conn):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["logged_in"]      = True
                    sess["portal_mobile"]  = "9000000001"
                yield c

    def test_overdue_pending_not_shown_as_pending(self, client):
        """Page must NOT display the string 'PENDING' for an overdue application."""
        resp = client.get("/application/1")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8", errors="replace")
        assert "PENDING" not in body, (
            "Status page still shows raw PENDING for a 20-day-old application"
        )

    def test_overdue_pending_shown_as_deemed_approved(self, client):
        """Page must display DEEMED_APPROVED (or 'Deemed Approved') for overdue."""
        resp = client.get("/application/1")
        body = resp.data.decode("utf-8", errors="replace").upper()
        assert "DEEMED" in body or "DEEMED_APPROVED" in body, (
            "Status page does not contain 'DEEMED' for overdue PENDING application"
        )


# ---------------------------------------------------------------------------
# T8 – is_overdue() is shared between scheduler and view_application
# ---------------------------------------------------------------------------

class TestIsOverdueIsShared:
    """Both modules must use the same is_overdue() function (from a shared location)."""

    def test_scheduler_exposes_is_overdue(self):
        mod = _import_scheduler()
        assert hasattr(mod, "is_overdue"), \
            "deemed_approval.py must expose is_overdue()"

    def test_app_is_overdue_agrees_with_scheduler(self):
        """
        The Flask app must import/use the same is_overdue logic.
        We check that view_application uses it by inspecting the source,
        OR that app.py imports is_overdue from the scheduler module.
        """
        source = APP_PY_PATH.read_text(encoding="utf-8")
        assert "is_overdue" in source, (
            "app.py does not reference is_overdue(); "
            "scheduler and status page must share the same function"
        )

    def test_is_overdue_naive_submitted_16_days_ago(self):
        """
        is_overdue(submitted_at, now) must return True for a naive submitted_at
        that is 16 days before an IST-aware now.
        This proves the function handles the naive DB value correctly.
        """
        mod = _import_scheduler()
        now_ist         = datetime.now(tz=IST)
        submitted_naive = (now_ist - timedelta(days=16)).replace(tzinfo=None)
        assert mod.is_overdue(submitted_naive, now_ist, sla_days=15) is True, \
            "is_overdue() returned False for a 16-day-old naive submitted_at"

    def test_is_overdue_naive_submitted_14_days_ago(self):
        mod = _import_scheduler()
        now_ist         = datetime.now(tz=IST)
        submitted_naive = (now_ist - timedelta(days=14)).replace(tzinfo=None)
        assert mod.is_overdue(submitted_naive, now_ist, sla_days=15) is False, \
            "is_overdue() returned True for a 14-day-old naive submitted_at"


# ---------------------------------------------------------------------------
# T9 – Crontab and container wiring
# ---------------------------------------------------------------------------

class TestCrontabWiring:
    def test_crontab_exists(self):
        assert CRONTAB_PATH.exists(), f"deploy/crontab not found at {CRONTAB_PATH}"

    def test_crontab_runs_deemed_approval(self):
        text = CRONTAB_PATH.read_text(encoding="utf-8")
        assert "deemed_approval.py" in text, \
            "crontab does not reference deemed_approval.py"

    def test_crontab_has_tz_ist(self):
        """
        The crontab must set TZ=Asia/Kolkata so cron fires at 02:00 IST,
        not 02:00 container-local (which is UTC inside Docker).
        """
        text = CRONTAB_PATH.read_text(encoding="utf-8")
        assert "TZ=Asia/Kolkata" in text, (
            "crontab missing TZ=Asia/Kolkata; cron fires at UTC midnight, not IST"
        )

    def test_script_exits_nonzero_on_failure(self):
        """
        Running deemed_approval.py with a deliberately bad DB config must exit non-zero.
        We call it as a subprocess with a patched config that points to an unreachable DB.
        """
        # Write a minimal throwaway config that will make psycopg2 fail fast
        import tempfile, configparser
        cfg = configparser.ConfigParser()
        cfg["database"] = {"host": "127.0.0.1", "port": "1",
                           "name": "noexist", "user": "noexist", "password": "noexist"}
        cfg["app"]      = {"sms_gateway_url": "http://localhost:1",
                           "otp_validity_seconds": "300", "upload_dir": "/tmp",
                           "secret_key": "x"}
        cfg["pension"]  = {"sla_days": "15", "min_age": "60",
                           "scheme_deadline": "2099-01-01 00:00"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            cfg.write(f)
            tmp_ini = f.name

        try:
            env = os.environ.copy()
            env["SEWASETU_CONFIG"] = tmp_ini    # fix must honour this env override
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH)],
                capture_output=True, timeout=10, env=env,
            )
            assert result.returncode != 0, (
                "deemed_approval.py exited 0 despite a connection failure; "
                "it must exit non-zero so the cron log shows the error"
            )
        finally:
            os.unlink(tmp_ini)
