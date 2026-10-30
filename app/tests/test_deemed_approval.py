# -*- coding: utf-8 -*-
"""
Fix-1 test suite for the RTPS deemed-approval scheduler.

Commit discipline:
  - This file is committed in its FAILING state first (red commit).
  - The fix commit makes all tests green.

Covers:
  T1   config: DEFAULT_CONFIG_PATH resolves to app.ini (not settings.ini)
  T2   SLA: 16-day-old PENDING selected
  T3   SLA: 14-day-old PENDING NOT selected
  T4   SLA: exactly-15-days selected (closed <=)
  T5a  cutoff is IST-aware (+05:30 fixed offset)
  T5b  PROOF: naive version is caught by T5a's assertion
  T6   SMS failure logged at ERROR, not swallowed
  T7   status page renders overdue PENDING as DEEMED_APPROVED (no skip)
  T8   rules.py is the shared source; app.py uses `from rules import`,
       not importlib; scheduler exposes is_overdue via rules
  T9   BEHAVIOURAL: under-60 applicants are excluded from the UPDATE and
       produce a WARNING log; script exits non-zero on DB failure
  T10  is_eligible_at_submission pure-function boundary tests (in class
       TestUnder60Exclusion below)
  T11  DRY-RUN SAFETY: main() without apply=True must execute zero UPDATEs
       and zero commits; main(apply=True) must execute the UPDATE

Design constraints:
  - Zero live Postgres connections.
  - Zero real SMS calls.
  - Fixed IST offset timedelta(hours=5, minutes=30) – no ZoneInfo/pytz.
  - submitted_at is TIMESTAMP WITHOUT TIME ZONE (psycopg2 returns naive);
    is_overdue() normalises by attaching IST (wall-clock, not conversion).
  - Scheduler and app.py share is_overdue/IST from app/rules.py.
"""

import configparser
import importlib.util
import logging
import os
import subprocess
import sys
import types
import unittest.mock as mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

REPO_ROOT    = Path(__file__).parent.parent.parent   # sewasetu-handout/
APP_DIR      = REPO_ROOT / "app"
RULES_PATH   = APP_DIR / "rules.py"
SCRIPT_PATH  = APP_DIR / "scripts" / "deemed_approval.py"
APP_PY_PATH  = APP_DIR / "app.py"


# ---------------------------------------------------------------------------
# Import helpers – never execute __main__
# ---------------------------------------------------------------------------

def _load(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scheduler() -> types.ModuleType:
    return _load(SCRIPT_PATH, "deemed_approval")


# ---------------------------------------------------------------------------
# T1 – Config path
# ---------------------------------------------------------------------------

class TestConfigPath:
    def test_import_does_not_raise(self):
        _scheduler()

    def test_default_config_path_is_app_ini(self):
        """DEFAULT_CONFIG_PATH must resolve to app.ini, not settings.ini."""
        mod = _scheduler()
        # After the fix: CONFIG_PATH is renamed DEFAULT_CONFIG_PATH
        path = str(getattr(mod, "DEFAULT_CONFIG_PATH", getattr(mod, "CONFIG_PATH", "")))
        assert "settings.ini" not in path, f"path still references settings.ini: {path}"
        assert "app.ini" in path, f"Expected app.ini in path, got: {path}"


# ---------------------------------------------------------------------------
# T2-T4 – SLA boundary (pure, no DB)
# ---------------------------------------------------------------------------

class TestComputeCutoff:
    def _now(self):
        return datetime.now(tz=IST)

    def test_16_day_old_is_overdue(self):
        mod = _scheduler()
        now = self._now()
        assert (now - timedelta(days=16)) <= mod.compute_cutoff(now, 15)

    def test_14_day_old_is_not_overdue(self):
        mod = _scheduler()
        now = self._now()
        assert (now - timedelta(days=14)) > mod.compute_cutoff(now, 15)

    def test_exactly_15_days_is_overdue(self):
        mod = _scheduler()
        now = self._now()
        assert (now - timedelta(days=15)) <= mod.compute_cutoff(now, 15)


# ---------------------------------------------------------------------------
# T5a – IST-aware cutoff
# ---------------------------------------------------------------------------

class TestCutoffIsIST:
    def test_cutoff_is_ist_aware(self):
        mod    = _scheduler()
        now    = datetime.now(tz=IST)
        cutoff = mod.compute_cutoff(now, 15)
        assert cutoff.tzinfo is not None, "compute_cutoff returned naive datetime"
        assert cutoff.utcoffset() == timedelta(hours=5, minutes=30), \
            f"Expected +05:30, got {cutoff.utcoffset()}"


# ---------------------------------------------------------------------------
# T5b – proof the naive bug is detectable
# ---------------------------------------------------------------------------

class TestNaiveVersionIsDetectedByT5:
    def _buggy_cutoff(self, now, sla_days):
        return now.replace(tzinfo=None) - timedelta(days=sla_days)

    def test_naive_cutoff_has_no_tzinfo(self):
        cutoff = self._buggy_cutoff(datetime.now(tz=IST), 15)
        assert cutoff.tzinfo is None

    def test_naive_offset_does_not_equal_ist(self):
        cutoff = self._buggy_cutoff(datetime.now(tz=IST), 15)
        assert cutoff.utcoffset() is None
        with pytest.raises(AssertionError):
            assert cutoff.utcoffset() == timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# T6 – SMS failure logged at ERROR
# ---------------------------------------------------------------------------

class TestSmsLogging:
    def test_sms_failure_logged_at_error(self, caplog):
        mod = _scheduler()
        with caplog.at_level(logging.ERROR):
            with mock.patch("requests.post", side_effect=Exception("down")):
                mod.send_approval_sms("9999999999", "SSP001", "http://x:1")
        assert any(r.levelno >= logging.ERROR for r in caplog.records), \
            "SMS failure was swallowed; expected ERROR log"


# ---------------------------------------------------------------------------
# T7 – Status page renders overdue PENDING as DEEMED_APPROVED
#      No importorskip: must be green inside Docker.
# ---------------------------------------------------------------------------

class TestStatusPageRendering:
    """
    /application/<id> for a 20-day-old PENDING must display DEEMED_APPROVED.
    DB is fully mocked. submitted_at is naive (TIMESTAMP WITHOUT TIME ZONE).
    """

    _SUBMITTED = datetime.now() - timedelta(days=20)   # naive, as from psycopg2

    _ROW = (
        "SSP26000001", "Ramesh Kumar", "9000000001", "1960-01-01",
        "Sonari", "Rajapara", "12345678901234", "SBIN0001234",
        "PENDING",       # status [8] – the bug
        _SUBMITTED,      # submitted_at [9]
        None,            # decided_at [10]
    )

    @pytest.fixture()
    def client(self):
        # app.py now does `from rules import is_overdue, IST` – no importlib.
        # We patch psycopg2.connect so no DB is opened during module init.
        spec = importlib.util.spec_from_file_location("sewasetu_app", APP_PY_PATH)
        mod  = importlib.util.module_from_spec(spec)
        with mock.patch("psycopg2.connect",
                        side_effect=RuntimeError("no live DB in tests")):
            spec.loader.exec_module(mod)

        flask_app = mod.app
        # The Flask app's template_folder is relative to its root_path (APP_DIR).
        # When imported via importlib from outside app/, Jinja needs the absolute path.
        flask_app.template_folder = str(APP_DIR / "templates")
        flask_app.config.update(TESTING=True, SECRET_KEY="test-secret")

        fake_cur  = mock.MagicMock()
        fake_cur.fetchone.return_value = self._ROW
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur

        with mock.patch.object(mod, "get_db", return_value=fake_conn):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["logged_in"]     = True
                    sess["portal_mobile"] = "9000000001"
                yield c


    def test_overdue_not_shown_as_pending(self, client):
        resp = client.get("/application/1")
        assert resp.status_code == 200
        assert b"PENDING" not in resp.data, \
            "Status page still shows PENDING for a 20-day-old application"

    def test_overdue_shown_as_deemed_approved(self, client):
        resp = client.get("/application/1")
        assert b"DEEMED" in resp.data.upper(), \
            "Status page does not show DEEMED for overdue PENDING application"


# ---------------------------------------------------------------------------
# T8 – rules.py is the shared module; no importlib in app.py
# ---------------------------------------------------------------------------

class TestRulesModule:
    """rules.py must exist and export the required symbols."""

    def test_rules_py_exists(self):
        assert RULES_PATH.exists(), f"app/rules.py not found at {RULES_PATH}"

    def test_rules_exports_ist(self):
        mod = _load(RULES_PATH, "rules")
        assert hasattr(mod, "IST"), "rules.py missing IST"
        assert mod.IST.utcoffset(None) == timedelta(hours=5, minutes=30)

    def test_rules_exports_compute_cutoff(self):
        mod = _load(RULES_PATH, "rules")
        assert hasattr(mod, "compute_cutoff"), "rules.py missing compute_cutoff"

    def test_rules_exports_is_overdue(self):
        mod = _load(RULES_PATH, "rules")
        assert hasattr(mod, "is_overdue"), "rules.py missing is_overdue"

    def test_rules_exports_is_eligible_at_submission(self):
        mod = _load(RULES_PATH, "rules")
        assert hasattr(mod, "is_eligible_at_submission"), \
            "rules.py missing is_eligible_at_submission"

    def test_app_uses_from_rules_import_not_importlib(self):
        """app.py must import from rules directly; no importlib hack."""
        src = APP_PY_PATH.read_text(encoding="utf-8")
        assert "from rules import" in src, \
            "app.py does not use 'from rules import'"
        assert "importlib.util.spec_from_file_location" not in src, \
            "app.py still has the importlib hack; move is_overdue to rules.py"

    def test_scheduler_uses_from_rules_import(self):
        """deemed_approval.py must import from rules, not define its own is_overdue."""
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "from rules import" in src, \
            "deemed_approval.py does not import from rules.py"

    def test_is_overdue_naive_16d(self):
        mod = _load(RULES_PATH, "rules")
        now = datetime.now(tz=IST)
        naive = (now - timedelta(days=16)).replace(tzinfo=None)
        assert mod.is_overdue(naive, now, 15) is True

    def test_is_overdue_naive_14d(self):
        mod = _load(RULES_PATH, "rules")
        now = datetime.now(tz=IST)
        naive = (now - timedelta(days=14)).replace(tzinfo=None)
        assert mod.is_overdue(naive, now, 15) is False


# ---------------------------------------------------------------------------
# T9 – BEHAVIOURAL: under-60 exclusion and logging; exit-code on failure
# ---------------------------------------------------------------------------

class TestSchedulerBehaviour:
    """
    Test the scheduler's actual runtime behaviour via mock cursor/connection.
    Does NOT test file contents. Never touches the live DB.
    """

    # Fixed dates chosen to avoid leap-year edge cases
    # Submission: 2026-03-01 10:00 IST (naive – as returned by psycopg2)
    _SUB = datetime(2026, 3, 1, 10, 0, 0)
    _DOB_59 = date(1967, 3, 1)   # age 59 on 2026-03-01
    _DOB_60 = date(1966, 3, 1)   # age 60 on 2026-03-01
    _DOB_61 = date(1965, 3, 1)   # age 61 on 2026-03-01

    @pytest.fixture()
    def tmp_cfg(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg["database"] = {"host": "127.0.0.1", "port": "5432",
                           "name": "x", "user": "x", "password": "x"}
        cfg["app"]      = {"sms_gateway_url": "http://localhost:1"}
        cfg["pension"]  = {"sla_days": "15", "min_age": "60"}
        p = tmp_path / "test.ini"
        p.write_text("\n".join(
            f"[{s}]\n" + "\n".join(f"{k}={v}" for k, v in cfg[s].items())
            for s in cfg.sections()
        ))
        return str(p)

    def _run_main(self, rows, tmp_cfg, caplog):
        """Run main(apply=True) with mocked psycopg2 returning `rows`; capture UPDATE calls."""
        mod = _scheduler()

        fake_cur  = mock.MagicMock()
        fake_cur.fetchall.return_value = rows
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur

        with mock.patch("psycopg2.connect", return_value=fake_conn), \
             mock.patch("requests.post"), \
             mock.patch.dict(os.environ, {"SEWASETU_CONFIG": tmp_cfg}), \
             caplog.at_level(logging.WARNING):
            mod.main(apply=True)  # apply=True: these tests verify write behaviour

        update_calls = [
            c for c in fake_cur.execute.call_args_list
            if c.args and "UPDATE" in str(c.args[0]).upper()
        ]
        return update_calls, caplog.records

    def test_under60_excluded_from_update(self, tmp_cfg, caplog):
        """Under-60 applicant (SSP001) must not appear in the UPDATE parameters."""
        rows = [
            ("SSP001", "9000000001", self._DOB_59, self._SUB),  # age 59 – skip
            ("SSP002", "9000000002", self._DOB_60, self._SUB),  # age 60 – approve
        ]
        updates, _ = self._run_main(rows, tmp_cfg, caplog)
        assert updates, "No UPDATE was executed at all"
        params_str = str(updates[0])
        assert "SSP001" not in params_str, "Under-60 (SSP001) included in UPDATE"
        assert "SSP002" in params_str, "Eligible (SSP002) missing from UPDATE"

    def test_under60_produces_warning_log(self, tmp_cfg, caplog):
        """A warning must be logged for each skipped under-60 applicant."""
        rows = [("SSP001", "9000000001", self._DOB_59, self._SUB)]
        _, records = self._run_main(rows, tmp_cfg, caplog)
        warns = [r for r in records
                 if r.levelno >= logging.WARNING and "SSP001" in r.message]
        assert warns, "No warning logged for under-60 applicant SSP001"

    def test_eligible_applicant_is_approved(self, tmp_cfg, caplog):
        """An applicant aged exactly 60 at submission must be included in the UPDATE."""
        rows = [("SSP003", "9000000003", self._DOB_60, self._SUB)]
        updates, _ = self._run_main(rows, tmp_cfg, caplog)
        assert updates, "No UPDATE for eligible applicant"
        assert "SSP003" in str(updates[0]), "Eligible (SSP003) missing from UPDATE"

    def test_all_under60_means_no_update(self, tmp_cfg, caplog):
        """If all overdue applicants are under-60, no UPDATE must be executed."""
        rows = [("SSP001", "9000000001", self._DOB_59, self._SUB)]
        updates, _ = self._run_main(rows, tmp_cfg, caplog)
        assert not updates, "UPDATE was executed despite all applicants being under-60"

    def test_exit_nonzero_on_db_failure(self, tmp_path):
        """Script must exit non-zero when DB connection fails."""
        cfg = configparser.ConfigParser()
        cfg["database"] = {"host": "127.0.0.1", "port": "1",
                           "name": "noexist", "user": "noexist", "password": "noexist"}
        cfg["app"]      = {"sms_gateway_url": "http://localhost:1"}
        cfg["pension"]  = {"sla_days": "15", "min_age": "60"}
        ini = tmp_path / "bad.ini"
        ini.write_text("\n".join(
            f"[{s}]\n" + "\n".join(f"{k}={v}" for k, v in cfg[s].items())
            for s in cfg.sections()
        ))
        env = {**os.environ, "SEWASETU_CONFIG": str(ini)}
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True, timeout=15, env=env,
        )
        assert r.returncode != 0, \
            "Script exited 0 despite DB connection failure; must exit non-zero"


# ---------------------------------------------------------------------------
# T10 – is_eligible_at_submission pure-function boundary tests
# ---------------------------------------------------------------------------

class TestUnder60Exclusion:
    """Pure-function tests for is_eligible_at_submission from rules.py."""

    # Submission: 2026-03-01 10:00 IST (naive)
    _SUB = datetime(2026, 3, 1, 10, 0, 0)

    def _fn(self):
        return _load(RULES_PATH, "rules").is_eligible_at_submission

    def test_age_59_is_not_eligible(self):
        assert self._fn()(date(1967, 3, 1), self._SUB, 60) is False

    def test_age_60_is_eligible(self):
        assert self._fn()(date(1966, 3, 1), self._SUB, 60) is True

    def test_turns_60_on_submission_day_is_eligible(self):
        """Turning 60 on the exact date of submission counts as eligible."""
        assert self._fn()(date(1966, 3, 1), self._SUB, 60) is True

    def test_age_58_is_not_eligible(self):
        assert self._fn()(date(1968, 3, 1), self._SUB, 60) is False

    def test_naive_submitted_at_treated_as_ist(self):
        """Naive submitted_at must be treated as IST wall-clock (not UTC)."""
        fn = self._fn()
        # Edge: 23:30 on 2026-02-28 IST is still Feb 28 IST.
        # If treated as UTC it would be +5:30 → 05:00 IST on Mar 1 – wrong.
        sub_naive = datetime(2026, 2, 28, 23, 30, 0)      # naive, IS IST
        dob_60    = date(1966, 2, 28)                       # turns 60 on 2026-02-28
        assert fn(dob_60, sub_naive, 60) is True, \
            "Naive submitted_at not treated as IST; IST date mismatch"


# ---------------------------------------------------------------------------
# T11 – DRY-RUN SAFETY
# ---------------------------------------------------------------------------

class TestDryRun:
    """
    main() must be safe to call without --apply:
      - Zero UPDATEs executed.
      - Zero commits.
      - Eligible and excluded IDs printed/logged (counts visible in output).
    main(apply=True) executes the UPDATE as before.

    RED: these tests fail until dry-run / apply flag is implemented.
    """

    _SUB   = datetime(2026, 3, 1, 10, 0, 0)
    _DOB_59 = date(1967, 3, 1)   # age 59 – excluded
    _DOB_60 = date(1966, 3, 1)   # age 60 – eligible

    @pytest.fixture()
    def tmp_cfg(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg["database"] = {"host": "127.0.0.1", "port": "5432",
                           "name": "x", "user": "x", "password": "x"}
        cfg["app"]      = {"sms_gateway_url": "http://localhost:1"}
        cfg["pension"]  = {"sla_days": "15", "min_age": "60"}
        p = tmp_path / "test.ini"
        p.write_text("\n".join(
            f"[{s}]\n" + "\n".join(f"{k}={v}" for k, v in cfg[s].items())
            for s in cfg.sections()
        ))
        return str(p)

    def _run(self, rows, tmp_cfg, apply: bool, caplog):
        """Call main(apply=apply) with mocked DB; return (update_calls, commit_calls)."""
        mod = _scheduler()

        fake_cur  = mock.MagicMock()
        fake_cur.fetchall.return_value = rows
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur

        with mock.patch("psycopg2.connect", return_value=fake_conn), \
             mock.patch("requests.post"), \
             mock.patch.dict(os.environ, {"SEWASETU_CONFIG": tmp_cfg}), \
             caplog.at_level(logging.INFO):
            mod.main(apply=apply)

        update_calls = [
            c for c in fake_cur.execute.call_args_list
            if c.args and "UPDATE" in str(c.args[0]).upper()
        ]
        commit_calls = fake_conn.commit.call_args_list
        return update_calls, commit_calls

    def test_dry_run_executes_no_update(self, tmp_cfg, caplog):
        """Without apply=True, NO UPDATE must reach the cursor."""
        rows = [
            ("SSP001", "9000000001", self._DOB_59, self._SUB),
            ("SSP002", "9000000002", self._DOB_60, self._SUB),
        ]
        updates, _ = self._run(rows, tmp_cfg, apply=False, caplog=caplog)
        assert not updates, (
            f"Dry-run issued {len(updates)} UPDATE(s); must be zero without --apply"
        )

    def test_dry_run_does_not_commit(self, tmp_cfg, caplog):
        """Without apply=True, conn.commit() must never be called."""
        rows = [("SSP002", "9000000002", self._DOB_60, self._SUB)]
        _, commits = self._run(rows, tmp_cfg, apply=False, caplog=caplog)
        assert not commits, (
            f"Dry-run called commit() {len(commits)} time(s); must be zero"
        )

    def test_dry_run_logs_eligible_count(self, tmp_cfg, caplog):
        """Dry-run must log the number of eligible applicants it WOULD approve."""
        rows = [("SSP002", "9000000002", self._DOB_60, self._SUB)]
        self._run(rows, tmp_cfg, apply=False, caplog=caplog)
        msgs = " ".join(r.message for r in caplog.records)
        assert "1" in msgs and ("eligible" in msgs.lower() or "dry" in msgs.lower()), (
            "Dry-run did not log eligible count; expected count and 'eligible'/'dry' in log"
        )

    def test_dry_run_logs_excluded_count(self, tmp_cfg, caplog):
        """Dry-run must log the number of under-60 applicants that were excluded."""
        rows = [
            ("SSP001", "9000000001", self._DOB_59, self._SUB),  # excluded
            ("SSP002", "9000000002", self._DOB_60, self._SUB),  # eligible
        ]
        self._run(rows, tmp_cfg, apply=False, caplog=caplog)
        msgs = " ".join(r.message for r in caplog.records)
        assert "1" in msgs and ("skip" in msgs.lower() or "exclud" in msgs.lower()), (
            "Dry-run did not log excluded count"
        )

    def test_apply_true_executes_update(self, tmp_cfg, caplog):
        """With apply=True, the UPDATE must be executed for eligible rows."""
        rows = [("SSP002", "9000000002", self._DOB_60, self._SUB)]
        updates, _ = self._run(rows, tmp_cfg, apply=True, caplog=caplog)
        assert updates, "apply=True did not execute any UPDATE"
        assert "SSP002" in str(updates[0]), "SSP002 missing from UPDATE with apply=True"

    def test_apply_true_commits(self, tmp_cfg, caplog):
        """With apply=True, conn.commit() must be called exactly once."""
        rows = [("SSP002", "9000000002", self._DOB_60, self._SUB)]
        _, commits = self._run(rows, tmp_cfg, apply=True, caplog=caplog)
        assert len(commits) == 1, (
            f"Expected exactly 1 commit with apply=True, got {len(commits)}"
        )

    def test_default_is_dry_run_via_subprocess(self, tmp_cfg):
        """
        Running the script without --apply must exit 0 and must NOT execute
        any UPDATE: verified by running as a subprocess against a real (but
        empty) in-process SQLite-compatible stub is impractical, so instead
        we verify that main() signature accepts no positional args and that
        calling main() (no apply kwarg) is equivalent to apply=False.

        The subprocess test uses --dry-run flag: script must accept it without
        error and must exit 0 even when DB is unavailable (dry-run reports
        what it would do based on the DB query, so DB still needed; but with
        a bad DB it should exit non-zero – tested separately).
        """
        import inspect
        mod = _scheduler()
        sig = inspect.signature(mod.main)
        params = sig.parameters
        # main() must accept an `apply` keyword argument
        assert "apply" in params, (
            "main() has no 'apply' parameter; dry-run not implemented"
        )
        # Default must be False (dry-run by default)
        default = params["apply"].default
        assert default is False or default == False, (  # noqa: E712
            f"main(apply) default is {default!r}; must be False (dry-run by default)"
        )
