# -*- coding: utf-8 -*-
"""
Task 3 – Post-submission acknowledgement download (no citizen login).

After a NEW application is submitted the confirmation page shows a
"Download Acknowledgment (PDF)" link.  The applicant has NOT gone through
the status-portal login, so session["logged_in"] is not set.

This must work WITHOUT requiring the citizen to log in separately, because:
  - The citizen just proved identity via mobile OTP during the apply flow.
  - Forcing a second login immediately after submission is hostile UX and
    will cause support calls / tout exploitation.

Fix approach: handle_submission() sets session["just_submitted_id"] = new_id.
              ack_pdf() checks this flag as a one-time authorization; the flag
              is cleared (popped) after it is used.

RED tests (fail before fix, pass after):
  T-ACK-1  ack_pdf must allow access when session["just_submitted_id"] matches
  T-ACK-2  ack_pdf must reject a mismatched just_submitted_id (wrong id)
  T-ACK-3  just_submitted_id is one-time: second fetch returns 302/403
  T-ACK-4  handle_submission sets just_submitted_id in session
"""

import importlib.util
import os
import types
import unittest.mock as mock
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
APP_DIR   = REPO_ROOT / "app"
APP_PY    = APP_DIR / "app.py"

NEW_APP_ID     = 42
NEW_APP_MOBILE = "9876543210"


def _load_app() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sewasetu_app_ack", APP_PY)
    mod  = importlib.util.module_from_spec(spec)
    env_patch = {
        "SEWASETU_SECRET_KEY":     "test-secret-key-ack",
        "SEWASETU_ADMIN_PASSWORD": "test-admin-pw-ack",
    }
    with mock.patch("psycopg2.connect",
                    side_effect=RuntimeError("no live DB in tests")), \
         mock.patch.dict(os.environ, env_patch):
        spec.loader.exec_module(mod)
    flask_app = mod.app
    flask_app.template_folder = str(APP_DIR / "templates")
    flask_app.config.update(TESTING=True, SECRET_KEY="test-secret-ack")
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_app()


def _fake_db_for_ack():
    """Fake DB: SELECT mobile FROM applications WHERE id=42 -> 9876543210."""
    fake_cur  = mock.MagicMock()
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_cur.fetchone.return_value = (NEW_APP_MOBILE,)
    return fake_conn, fake_cur


class TestAckPdfWithSubmitToken:
    """just_submitted_id session flag must grant one-time PDF access."""

    def test_matching_token_grants_200(self, mod):
        """T-ACK-1: just_submitted_id == app_id and no portal login -> 200."""
        flask_app = mod.app
        fake_conn, _ = _fake_db_for_ack()
        fake_pdf = b"%PDF-1.4 fake"

        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("flask.send_file",
                        return_value=flask_app.response_class(
                            response=fake_pdf, status=200,
                            mimetype="application/pdf")):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    # NOT logged_in, NOT admin – only just_submitted_id
                    sess["just_submitted_id"] = NEW_APP_ID
                resp = c.get(f"/ack/{NEW_APP_ID}.pdf")

        assert resp.status_code == 200, (
            f"Expected 200 when just_submitted_id matches, got {resp.status_code}. "
            "IDOR fix broke post-submission PDF download. "
            "Fix: ack_pdf() must honour session['just_submitted_id'] == app_id."
        )

    def test_mismatched_token_rejected(self, mod):
        """T-ACK-2: just_submitted_id=42 but requesting /ack/99.pdf -> 302/403."""
        flask_app = mod.app
        fake_conn, _ = _fake_db_for_ack()

        with mock.patch.object(mod, "get_db", return_value=fake_conn):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["just_submitted_id"] = NEW_APP_ID  # 42, not 99
                resp = c.get("/ack/99.pdf")

        assert resp.status_code in (302, 403, 404), (
            f"Expected 302/403/404 when token id mismatches, got {resp.status_code}."
        )

    def test_token_is_one_time(self, mod):
        """T-ACK-3: second fetch after token consumed -> 302/403."""
        flask_app = mod.app
        fake_conn, _ = _fake_db_for_ack()
        fake_pdf = b"%PDF-1.4 fake"

        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("flask.send_file",
                        return_value=flask_app.response_class(
                            response=fake_pdf, status=200,
                            mimetype="application/pdf")):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["just_submitted_id"] = NEW_APP_ID
                resp1 = c.get(f"/ack/{NEW_APP_ID}.pdf")
                resp2 = c.get(f"/ack/{NEW_APP_ID}.pdf")

        assert resp1.status_code == 200, (
            f"First fetch should return 200, got {resp1.status_code}"
        )
        assert resp2.status_code in (302, 403), (
            f"T-ACK-3: second fetch must be rejected after token is consumed, "
            f"got {resp2.status_code}. Pop the key after first use."
        )

    def test_handle_submission_sets_token(self, mod):
        """T-ACK-4: handle_submission() must set session['just_submitted_id']."""
        src = APP_PY.read_text(encoding="utf-8")
        assert "just_submitted_id" in src, (
            "handle_submission() does not set session['just_submitted_id']. "
            "Citizens who just submitted cannot download their acknowledgement "
            "PDF without a separate login. "
            "Fix: add  session['just_submitted_id'] = new_id  in handle_submission() "
            "BEFORE clearing other session keys."
        )