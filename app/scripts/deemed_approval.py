# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron (TZ=Asia/Kolkata), 02:00 IST daily. See deploy/crontab.

Refactored for Fix 1:
  - All logic in named functions; no top-level side-effects beyond config load.
  - Config reads app/config/app.ini (single source of truth; env SEWASETU_CONFIG
    overrides the path for testing).
  - compute_cutoff() and is_overdue() are importable by app.py so the status
    page and the scheduler share identical boundary logic.
  - IST-aware cutoff: fixed offset +05:30, no ZoneInfo dependency.
  - submitted_at is TIMESTAMP WITHOUT TZ in the DB (psycopg2 returns naive);
    is_overdue() normalises it by treating it as IST-wall-clock time.
  - SMS failures are logged at ERROR level; they are never silently swallowed.
  - Script exits non-zero on any unhandled exception (sys.exit(1)).
"""

import logging
import os
import sys
import configparser
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

# ---------------------------------------------------------------------------
# IST constant – fixed offset, no ZoneInfo/pytz dependency
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Allow tests to override the config path via env var (never touches live DB).
CONFIG_PATH = os.environ.get(
    "SEWASETU_CONFIG",
    os.path.join(BASE_DIR, "config", "app.ini"),
)


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    with open(CONFIG_PATH) as fh:
        cfg.read_file(fh)
    return cfg


# ---------------------------------------------------------------------------
# Core logic – importable by app.py for the status page
# ---------------------------------------------------------------------------

def compute_cutoff(now: datetime, sla_days: int) -> datetime:
    """
    Return the latest submitted_at that is still within the SLA, as an
    IST-aware datetime.

    ``now`` must be IST-aware (caller's responsibility).  The returned
    cutoff carries the same +05:30 tzinfo so comparisons are unambiguous.
    """
    return now - timedelta(days=sla_days)


def is_overdue(submitted_at: datetime, now: datetime, sla_days: int = 15) -> bool:
    """
    Return True when an application has breached the RTPS SLA.

    ``submitted_at`` may be naive (as returned by psycopg2 from a TIMESTAMP
    WITHOUT TIME ZONE column).  We treat it as IST wall-clock time – because
    the application window closes at midnight IST and all timestamps in the DB
    were written by the Flask app running in IST context.

    ``now`` must be IST-aware.

    Boundary: exactly at the SLA edge is overdue (closed interval <=).
    """
    if submitted_at.tzinfo is None:
        # Naive → attach IST; do NOT convert (the stored wall-clock time IS IST)
        submitted_at = submitted_at.replace(tzinfo=IST)
    cutoff = compute_cutoff(now, sla_days)
    return submitted_at <= cutoff


# ---------------------------------------------------------------------------
# SMS helper – logs failures, never swallows them silently
# ---------------------------------------------------------------------------

def send_approval_sms(mobile: str, app_no: str, sms_gateway_url: str) -> None:
    """Send an RTPS deemed-approval notification.  Any exception is logged at ERROR."""
    try:
        requests.post(
            sms_gateway_url + "/api/send",
            json={
                "to": mobile,
                "text": (
                    "Sewa Setu: your pension application %s stands approved "
                    "under the RTPS Act." % app_no
                ),
            },
            timeout=5,
        )
    except Exception as exc:
        logging.error(
            "deemed_approval: SMS failed for application %s mobile %s: %s",
            app_no, mobile, exc,
        )


# ---------------------------------------------------------------------------
# Database query – parameterised SQL only
# ---------------------------------------------------------------------------

def find_overdue(cur, now: datetime, sla_days: int = 15):
    """
    Return list of (application_no, mobile) for PENDING rows that have
    breached the SLA.

    Uses parameterised SQL; the cutoff is computed in Python so the IST
    timezone handling is explicit and testable.
    """
    cutoff = compute_cutoff(now, sla_days)
    # Convert to UTC for the DB comparison so the TIMESTAMP column arithmetic
    # is unambiguous (psycopg2 sends an aware datetime → Postgres converts correctly).
    cutoff_utc = cutoff.astimezone(timezone.utc)
    cur.execute(
        """
        SELECT application_no, mobile
        FROM   applications
        WHERE  status       = %s
        AND    submitted_at <= %s
        """,
        ("PENDING", cutoff_utc),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s deemed_approval: %(message)s",
        level=logging.INFO,
    )

    cfg      = load_config()
    sla_days = cfg.getint("pension", "sla_days")
    sms_url  = cfg.get("app", "sms_gateway_url")

    conn = psycopg2.connect(
        host=cfg.get("database", "host"),
        port=cfg.get("database", "port"),
        dbname=cfg.get("database", "name"),
        user=cfg.get("database", "user"),
        password=cfg.get("database", "password"),
    )
    try:
        cur = conn.cursor()
        now = datetime.now(tz=IST)
        rows = find_overdue(cur, now, sla_days)

        cur.execute(
            """
            UPDATE applications
            SET    status      = %s,
                   decided_at  = %s,
                   decided_by  = %s
            WHERE  status      = %s
            AND    submitted_at <= %s
            """,
            (
                "DEEMED_APPROVED",
                now,                          # decided_at = run time
                "RTPS-AUTO",
                "PENDING",
                compute_cutoff(now, sla_days).astimezone(timezone.utc),
            ),
        )
        conn.commit()

        logging.info(
            "%s deemed_approval: %d application(s) approved",
            now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            len(rows),
        )

        for app_no, mobile in rows:
            send_approval_sms(mobile, app_no, sms_url)

    except Exception:
        conn.rollback()
        logging.exception("deemed_approval: unhandled error; rolled back")
        raise
    finally:
        cur.close()
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
