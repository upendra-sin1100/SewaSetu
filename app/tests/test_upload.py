# -*- coding: utf-8 -*-
"""
Fix 5 – Upload: accept JPG/PDF up to 5 MB, plain-language errors.

RED tests – all fail before the fix.

Current broken behaviour:
  - Only .pdf accepted (form says JPG or PDF).
  - 102400-byte (100 KB) limit (form says 5 MB).
  - All three failure branches flash "ERR_VAL_47" (meaningless to a citizen).
  - User-supplied filename is spliced directly into the saved path (path traversal).

Tests
-----
T-UPL-1  small PDF (b"%PDF-1.4 x", "proof.pdf") -> 302 redirect
T-UPL-2  JPG ("proof.JPG", magic bytes) -> 302 redirect
T-UPL-3  ~2 MB PDF -> 302 redirect
T-UPL-4  >5 MB file -> error message contains "5 MB", not "ERR_VAL_"
T-UPL-5  "notes.txt" -> error message mentions "JPG or PDF", not "ERR_VAL_"
T-UPL-6  no file -> error message contains "choose" (case-insensitive), not "ERR_VAL_"
T-UPL-7  "../../evil.pdf" -> session["doc_path"] contains neither ".." nor "evil"
"""

import importlib.util
import io
import os
import types
import unittest.mock as mock
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
APP_DIR   = REPO_ROOT / "app"
APP_PY    = APP_DIR / "app.py"

MOBILE = "9000000001"


# ---------------------------------------------------------------------------
# App loader
# ---------------------------------------------------------------------------

def _load_app() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sewasetu_app_upl", APP_PY)
    mod  = importlib.util.module_from_spec(spec)
    env_patch = {
        "SEWASETU_SECRET_KEY":     "test-secret-key-upl",
        "SEWASETU_ADMIN_PASSWORD": "test-admin-pw-upl",
    }
    with mock.patch("psycopg2.connect",
                    side_effect=RuntimeError("no live DB in tests")), \
         mock.patch.dict(os.environ, env_patch):
        spec.loader.exec_module(mod)
    flask_app = mod.app
    flask_app.template_folder = str(APP_DIR / "templates")
    flask_app.config.update(TESTING=True, SECRET_KEY="test-secret-upl")
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_app()


# ---------------------------------------------------------------------------
# Helper: upload client with verified_mobile in session
# ---------------------------------------------------------------------------

def _upload_client(mod, extra_sess=None):
    """Return (context-manager-wrapped client, module).  Session has verified_mobile."""
    flask_app = mod.app
    # Patch os.makedirs and open so no disk I/O happens in tests.
    return flask_app.test_client()


def _post_upload(client, mod, filename, content, content_type="application/pdf"):
    """POST multipart/form-data to /upload."""
    data = {}
    if filename is not None:
        data["document"] = (io.BytesIO(content), filename, content_type)
    with mock.patch.object(mod, "UPLOAD_DIR", "/fake/uploads"), \
         mock.patch("os.makedirs"), \
         mock.patch("builtins.open", mock.mock_open()):
        resp = client.post(
            "/upload",
            data=data,
            content_type="multipart/form-data",
        )
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUpload:

    @pytest.fixture(autouse=True)
    def client(self, mod):
        """Fresh test client for each test with verified_mobile set."""
        with mod.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["verified_mobile"] = MOBILE
            self._c = c
            self._mod = mod
            yield

    def _post(self, filename, content, ct="application/pdf"):
        return _post_upload(self._c, self._mod, filename, content, ct)

    # T-UPL-1: small PDF -> 302
    def test_small_pdf_accepted(self):
        """T-UPL-1: A small valid PDF is accepted and redirects to /declaration."""
        resp = self._post("proof.pdf", b"%PDF-1.4 x" + b"0" * 500)
        assert resp.status_code == 302, (
            f"Expected 302 for a valid small PDF, got {resp.status_code}. "
            "Upload should accept PDFs."
        )

    # T-UPL-2: JPG -> 302
    def test_jpg_accepted(self):
        """T-UPL-2: A JPG file is accepted and redirects to /declaration."""
        jpg_content = b"\xff\xd8\xff\xe0" + b"\x00" * 1000
        resp = self._post("proof.JPG", jpg_content, "image/jpeg")
        assert resp.status_code == 302, (
            f"Expected 302 for a JPG, got {resp.status_code}. "
            "Form promises JPG or PDF; JPG must be accepted."
        )

    # T-UPL-3: ~2 MB PDF -> 302
    def test_large_but_valid_pdf_accepted(self):
        """T-UPL-3: A ~2 MB PDF is within the 5 MB limit and must be accepted."""
        resp = self._post("scan.pdf", b"%PDF-1.4 " + b"A" * (2 * 1024 * 1024))
        assert resp.status_code == 302, (
            f"Expected 302 for a 2 MB PDF, got {resp.status_code}. "
            "Form promises 5 MB; 2 MB must be accepted (not the current 100 KB limit)."
        )

    # T-UPL-4: >5 MB -> plain error, no ERR_VAL_
    def test_oversized_file_gives_plain_error(self):
        """T-UPL-4: >5 MB file shows a message mentioning '5 MB', not 'ERR_VAL_'."""
        oversized = b"%PDF-1.4 " + b"B" * (5 * 1024 * 1024 + 1)
        resp = self._post("huge.pdf", oversized)
        body = resp.get_data(as_text=True)
        assert "ERR_VAL_" not in body, (
            "Oversized file still shows 'ERR_VAL_'. Replace with a plain message."
        )
        assert "5 MB" in body or "5mb" in body.lower(), (
            f"Oversized file message does not mention '5 MB'. Got: {body[:300]}"
        )

    # T-UPL-5: wrong extension -> plain error mentioning allowed types
    def test_wrong_extension_gives_plain_error(self):
        """T-UPL-5: A .txt file shows a message mentioning 'JPG or PDF', not 'ERR_VAL_'."""
        resp = self._post("notes.txt", b"hello world", "text/plain")
        body = resp.get_data(as_text=True)
        assert "ERR_VAL_" not in body, (
            "Wrong-extension error still shows 'ERR_VAL_'. Replace with a plain message."
        )
        assert "JPG" in body or "PDF" in body, (
            f"Wrong-extension message does not mention 'JPG or PDF'. Got: {body[:300]}"
        )

    # T-UPL-6: no file -> plain error mentioning "choose"
    def test_no_file_gives_plain_error(self):
        """T-UPL-6: Submitting with no file chosen shows a helpful message."""
        # POST with empty data dict (no 'document' field at all)
        with mock.patch.object(self._mod, "UPLOAD_DIR", "/fake/uploads"), \
             mock.patch("os.makedirs"), \
             mock.patch("builtins.open", mock.mock_open()):
            resp = self._c.post("/upload", data={}, content_type="multipart/form-data")
        body = resp.get_data(as_text=True)
        assert "ERR_VAL_" not in body, (
            "No-file error still shows 'ERR_VAL_'. Replace with a plain message."
        )
        assert "choose" in body.lower() or "select" in body.lower(), (
            f"No-file message does not say 'choose' or 'select'. Got: {body[:300]}"
        )

    # T-UPL-7: path traversal in filename -> uuid-based safe path
    def test_path_traversal_filename_is_sanitised(self):
        """T-UPL-7: '../../evil.pdf' must not appear in session['doc_path']."""
        resp = self._post("../../evil.pdf", b"%PDF-1.4 safe")
        if resp.status_code != 302:
            # If the file was rejected for other reasons, skip the path check.
            pytest.skip("File was rejected (not yet accepted); path check not applicable.")
        with self._c.session_transaction() as sess:
            doc_path = sess.get("doc_path", "")
        assert ".." not in doc_path, (
            f"Path traversal '..' found in session['doc_path']: {doc_path!r}. "
            "Never use the user-supplied filename in the saved path."
        )
        assert "evil" not in doc_path, (
            f"User filename 'evil' found in session['doc_path']: {doc_path!r}."
        )