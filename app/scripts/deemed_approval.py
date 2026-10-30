# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not
processed within the statutory SLA stands DEEMED_APPROVED.  This job marks
such applications and notifies the applicant by SMS.

Scheduled via cron (TZ=Asia/Kolkata), 02:00 IST daily. See deploy/crontab.

Fix-1 changes
-------------
* Config: reads app/config/app.ini (SEWASETU_CONFIG env var overrides path).
* Shared logic: imports is_overdue, compute_cutoff, is_eligible_at_submission,
  IST from app/rules.py.  No importlib hackery.
* submitted_at column type is TIMESTAMP WITHOUT TIME ZONE (naive from psycopg2).
  is_overdue() and is_eligible_at_submission() both treat naive values as IST
  wall-clock via .replace(tzinfo=IST) – not .astimezone().
  find_overdue() compares naive-IST to naive-IST in the DB query (strip tz
  from the cutoff before binding) to avoid server-timezone mismatch.
* Under-60 at submission (IST date) are excluded, logged at WARNING, and
  listed separately for DC review.
* SMS failures are logged at ERROR; never silently swallowed.
* Any unhandled exception → rollback → sys.exit(1) so cron log shows failure.

Fix-2 changes
-------------
* Dry-run by default: main(apply=False) is the safe default.  The job prints
  what it WOULD do (counts, eligible IDs, excluded IDs) but executes no UPDATE
  and no commit.  To write to the DB, pass --apply on the command line or call
  main(apply=True) in code.
* decided_at: set to submitted_at + sla_days (the statutory deadline date),
  NOT to the time the job runs.  The legal deemed-approval date is the day the
  SLA expired, not the day the cron job happened to notice it.  The nightly job
  uses the same formula; only the value of submitted_at differs per row.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

# ---------------------------------------------------------------------------
# Shared rules – imported from app/rules.py.
# sys.path is extended so this works when run as a script from any CWD
# (the Dockerfile WORKDIR is /app so `from rules import` works natively;
# the path manipulation is only needed when invoked as a standalone script
# from outside /app, e.g. during local development or tests).
# ---------------------------------------------------------------------------
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from rules import IST, compute_cutoff, is_overdue, is_eligible_at_submission  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.join(_APP_DIR, "config", "app.ini")


def load_config():
    import configparser
    path = os.environ.get("SEWASETU_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = configparser.ConfigParser()
    with open(path) as fh:
        cfg.read_file(fh)
    return cfg


# ---------------------------------------------------------------------------
# SMS – logs failures at ERROR, never swallows them
# ---------------------------------------------------------------------------

def send_approval_sms(mobile: str, app_no: str, sms_gateway_url: str) -> None:
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
            "deemed_approval: SMS failed for %s mobile %s: %s",
            app_no, mobile, exc,
        )


# ---------------------------------------------------------------------------
# DB query – parameterised SQL only; naive cutoff for naive column
# ---------------------------------------------------------------------------

def find_overdue(cur, now: datetime, sla_days: int = 15):
    """
    Return list of (application_no, mobile, dob, submitted_at) for PENDING
    rows that have breached the SLA.

    submitted_at is TIMESTAMP WITHOUT TIME ZONE → stored as IST wall-clock,
    returned as a naive datetime by psycopg2.  We compare naive-IST to
    naive-IST in the DB query (strip tz from the cutoff) so that the
    comparison is apples-to-apples and is not affected by the Postgres
    server's local timezone setting.
    """
    cutoff       = compute_cutoff(now, sla_days)
    cutoff_naive = cutoff.replace(tzinfo=None)   # strip tz: compare wall-clock to wall-clock
    cur.execute(
        """
        SELECT application_no, mobile, dob, submitted_at
        FROM   applications
        WHERE  status       = %s
        AND    submitted_at <= %s
        """,
        ("PENDING", cutoff_naive),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(apply: bool = False) -> int:
    """
    Run the deemed-approval job.

    Parameters
    ----------
    apply : bool
        False (default) → dry-run: query the DB, log what WOULD be done,
        execute no UPDATE, make no commit, send no SMS.
        True            → apply mode: execute the UPDATE, commit, send SMS.

    CLI equivalents
    ---------------
        python deemed_approval.py            # dry-run (safe)
        python deemed_approval.py --apply    # writes to the DB
    """
    logging.basicConfig(
        format="%(asctime)s %(levelname)s deemed_approval: %(message)s",
        level=logging.INFO,
    )

    mode = "APPLY" if apply else "DRY-RUN"
    logging.info("deemed_approval: starting in %s mode", mode)

    cfg      = load_config()
    sla_days = cfg.getint("pension", "sla_days")
    min_age  = cfg.getint("pension", "min_age")
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

        # 1. Find all overdue PENDING rows.
        rows = find_overdue(cur, now, sla_days)

        # 2. Split into eligible (age >= 60 at submission in IST) and skipped.
        eligible = []   # list of (app_no, mobile, submitted_at)
        skipped  = []
        for app_no, mobile, dob, submitted_at in rows:
            if is_eligible_at_submission(dob, submitted_at, min_age):
                eligible.append((app_no, mobile, submitted_at))
            else:
                logging.warning(
                    "deemed_approval: SKIPPING %s mobile=%s – age at submission < %d "
                    "(dob=%s submitted_at=%s IST); forwarded to DC for review",
                    app_no, mobile, min_age, dob, submitted_at,
                )
                skipped.append(app_no)

        if skipped:
            logging.info(
                "deemed_approval: %d application(s) skipped (under-%d at submission): %s",
                len(skipped), min_age, skipped,
            )

        logging.info(
            "deemed_approval: [%s] %d eligible, %d excluded (under-%d)",
            mode, len(eligible), len(skipped), min_age,
        )
        if eligible:
            logging.info(
                "deemed_approval: [%s] eligible ids: %s",
                mode, [r[0] for r in eligible],
            )

        if not apply:
            # ---------------------------------------------------------------
            # DRY-RUN: report only, no writes
            # ---------------------------------------------------------------
            logging.info(
                "deemed_approval: [DRY-RUN] would approve %d application(s). "
                "Re-run with --apply to write.",
                len(eligible),
            )
            return 0

        # 3. APPLY MODE: Update only eligible rows.
        #    decided_at = submitted_at + sla_days days (the statutory expiry
        #    date – the legal deemed-approval date is when the SLA expired,
        #    not when the cron job ran).  Parameterised SQL; IN %s with tuple.
        if eligible:
            app_nos      = tuple(r[0] for r in eligible)
            cutoff_naive = compute_cutoff(now, sla_days).replace(tzinfo=None)
            cur.execute(
                """
                UPDATE applications
                SET    status      = %s,
                       decided_at  = submitted_at + %s,
                       decided_by  = %s
                WHERE  status           = %s
                AND    application_no IN %s
                AND    submitted_at    <= %s
                """,
                (
                    "DEEMED_APPROVED",
                    timedelta(days=sla_days),   # row-level: each row's own deadline
                    "RTPS-AUTO",
                    "PENDING",
                    app_nos,
                    cutoff_naive,
                ),
            )
            conn.commit()
            logging.info(
                "deemed_approval: %s – %d application(s) approved",
                now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                len(eligible),
            )
        else:
            logging.info("deemed_approval: no eligible overdue applications to process")

        # 4. Send SMS (best-effort; errors already logged inside send_approval_sms).
        for app_no, mobile, _ in eligible:
            send_approval_sms(mobile, app_no, sms_url)

    except Exception:
        conn.rollback()
        logging.exception("deemed_approval: unhandled error; transaction rolled back")
        raise   # causes sys.exit(1) via the outer wrapper

    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="RTPS deemed-approval job.  Default is dry-run (safe)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write approved status to the DB and send SMS. Without this flag the "
             "job reports what it would do without making any changes.",
    )
    args = parser.parse_args()
    sys.exit(main(apply=args.apply))
