# -*- coding: utf-8 -*-
"""
Task 3 – Post-submission acknowledgement download (no citizen login).

RED tests (fail before fix, pass after):
  T-ACK-1  ack_pdf allows access when session["just_submitted_id"] matches
  T-ACK-2  ack_pdf rejects mismatched just_submitted_id (wrong id)
  T-ACK-3  TWO consecutive GETs of own PDF both return 200
           (updated: Chrome's PDF viewer fetches the URL twice; the old
            "second download = 302" behaviour broke the download button)
  T-ACK-4  handle_submission sets just_submitted_id in session (source check)
  T-ACK-5  GET /ack/<other id>.pdf with just_submitted_id=42 returns 403
           (the token must only authorise the exact app_id it was issued for)
"""

import importlib.util
import io
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
OTHER_APP_ID   = 99


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


def _fake_db_for_ack(app_id=NEW_APP_ID, mobile=NEW_APP_MOBILE):
    """Fake DB: SELECT mobile FROM applications WHERE id=<app_id> -> <mobile>."""
    fake_cur  = mock.MagicMock()
    fake_conn = mock.MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_cur.fetchone.return_value = (mobile,)
    return fake_conn, fake_cur


class TestAckPdfWithSubmitToken:
    """just_submitted_id session flag must grant persistent PDF access for own id."""

    def test_matching_token_grants_200(self, mod):
        """T-ACK-1: just_submitted_id == app_id and no portal login -> 200."""
        flask_app = mod.app
        fake_conn, _ = _fake_db_for_ack()
        fake_pdf = b"%PDF-1.4 fake"

        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch("os.path.exists", return_value=False), \
             mock.patch.object(mod, "generate_acknowledgment",
                               return_value=fake_pdf):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    # NOT logged_in, NOT admin - only just_submitted_id
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

    def test_two_consecutive_downloads_both_200(self, mod):
        """T-ACK-3: Two consecutive GETs of own PDF both return 200.

        Chrome's PDF viewer requests the file once to render it and again when
        the user clicks the download button.  Popping the session token on the
        first request causes the second request to get a 302-to-login, leaving
        an unconfirmed .crdownload file on the citizen's desktop.

        Fix: do NOT pop just_submitted_id; validate it without consuming it.
        """
        flask_app = mod.app
        fake_conn, _ = _fake_db_for_ack()
        fake_pdf = b"%PDF-1.4 fake"

        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch("os.path.exists", return_value=False), \
             mock.patch.object(mod, "generate_acknowledgment",
                               return_value=fake_pdf):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["just_submitted_id"] = NEW_APP_ID
                resp1 = c.get(f"/ack/{NEW_APP_ID}.pdf")
                resp2 = c.get(f"/ack/{NEW_APP_ID}.pdf")

        assert resp1.status_code == 200, (
            f"First fetch should return 200, got {resp1.status_code}"
        )
        assert resp2.status_code == 200, (
            f"T-ACK-3: second consecutive fetch must also return 200, "
            f"got {resp2.status_code}. "
            "Chrome's PDF viewer fetches the URL twice. "
            "Fix: do NOT pop just_submitted_id; check it without consuming it."
        )

    def test_handle_submission_sets_token(self, mod):
        """T-ACK-4: handle_submission() must set session['just_submitted_id']."""
        src = APP_PY.read_text(encoding="utf-8")
        assert "just_submitted_id" in src, (
            "handle_submission() does not set session['just_submitted_id']. "
            "Citizens who just submitted cannot download their acknowledgement "
            "PDF without a separate login. "
            "Fix: add  session['just_submitted_id'] = new_id  in handle_submission()."
        )

    def test_token_only_authorises_own_app_id(self, mod):
        """T-ACK-5: just_submitted_id=42 must NOT grant access to /ack/99.pdf.

        The token must be scoped to the exact application id it was issued for.
        A citizen must not be able to use their submission token to download
        another citizen's acknowledgement.
        """
        flask_app = mod.app
        # DB returns OTHER_APP_ID's mobile (different from session mobile)
        fake_cur  = mock.MagicMock()
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        fake_cur.fetchone.return_value = ("9999999999",)  # other citizen's mobile

        with mock.patch.object(mod, "get_db", return_value=fake_conn), \
             mock.patch("os.path.exists", return_value=True):
            with flask_app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["just_submitted_id"] = NEW_APP_ID  # token for id=42
                # Request a *different* id (99)
                resp = c.get(f"/ack/{OTHER_APP_ID}.pdf")

        assert resp.status_code in (302, 403, 404), (
            f"T-ACK-5: just_submitted_id=42 must not grant access to /ack/99.pdf, "
            f"got {resp.status_code}. "
            "The session token must only authorise the exact app_id it was issued for."
        )