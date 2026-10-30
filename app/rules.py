# -*- coding: utf-8 -*-
"""
app/rules.py  –  Shared business-rule functions for Sewa Setu.

Imported by:
  - app/app.py                  (Flask web application)
  - app/scripts/deemed_approval.py  (nightly RTPS scheduler)

Keep this module free of Flask, psycopg2, and I/O; pure Python only.
"""

import datetime as _dt
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# IST constant – fixed +05:30 offset; no ZoneInfo / pytz dependency.
# ---------------------------------------------------------------------------

IST: timezone = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# SLA helpers
# ---------------------------------------------------------------------------


def compute_cutoff(now: datetime, sla_days: int) -> datetime:
    """
    Return the latest submitted_at that has already breached the SLA,
    as an IST-aware datetime.

    ``now`` must be IST-aware (caller's responsibility).
    """
    return now - timedelta(days=sla_days)


def is_overdue(submitted_at: datetime, now: datetime, sla_days: int = 15) -> bool:
    """
    Return True when an application has breached the RTPS SLA.

    submitted_at column type: TIMESTAMP WITHOUT TIME ZONE.
    psycopg2 returns a *naive* datetime for this column type.
    We treat the stored wall-clock value as IST (the application window
    closes at midnight IST, and all timestamps were written in that context)
    by attaching the IST tzinfo via .replace() – NOT .astimezone(), which
    would convert from UTC.

    Boundary: exactly at the SLA edge counts as overdue (closed <=).
    """
    if submitted_at.tzinfo is None:
        # Attach IST; treat wall-clock as IST, do NOT shift.
        submitted_at = submitted_at.replace(tzinfo=IST)
    return submitted_at <= compute_cutoff(now, sla_days)


# ---------------------------------------------------------------------------
# Eligibility helpers
# ---------------------------------------------------------------------------


def age_on(dob: _dt.date, reference_date: _dt.date) -> int:
    """Completed years of age of dob on reference_date."""
    return (
        reference_date.year - dob.year
        - (
            (reference_date.month, reference_date.day)
            < (dob.month, dob.day)
        )
    )


def is_eligible_at_submission(
    dob: _dt.date,
    submitted_at: datetime,
    min_age: int = 60,
) -> bool:
    """
    Return True if the applicant was >= min_age years old on the IST date
    on which they submitted the application.

    submitted_at may be naive (TIMESTAMP WITHOUT TIME ZONE from psycopg2).
    Treated as IST wall-clock via .replace(tzinfo=IST).
    """
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=IST)
    submission_date_ist = submitted_at.astimezone(IST).date()
    return age_on(dob, submission_date_ist) >= min_age
