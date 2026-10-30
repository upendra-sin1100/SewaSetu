from pathlib import Path

CRON = Path(__file__).resolve().parents[1] / "deploy" / "crontab"


def _job_lines():
    return [l for l in CRON.read_text().splitlines()
            if "deemed_approval.py" in l and not l.lstrip().startswith("#")]


def test_nightly_job_is_scheduled():
    assert _job_lines(), "deemed_approval.py is not scheduled"


def test_nightly_job_runs_with_apply():
    # dry-run is the default, so without --apply the job approves nobody
    assert all("--apply" in l for l in _job_lines())


def test_crontab_has_lf_line_endings():
    assert b"\r" not in CRON.read_bytes()