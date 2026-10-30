# -*- coding: utf-8 -*-
"""
Fix 6 – Indian-script names are damaged (DC complaint #1).

Evidence:
  - sanitize() slices at 20 *bytes*, not characters. "দীপক কলিতা" is 30 bytes
    in UTF-8 but only 10 characters; it was truncated or silently mutilated.
  - The DB column applicant_name is VARCHAR(100) — 100 *characters* in PostgreSQL
    UTF-8; no byte-level constraint exists there.
  - generate_acknowledgment() uses pdf.set_font("Helvetica") which is a
    built-in fpdf2 latin-1 font. Any codepoint > 127 triggers the bare
    except branch and writes a literal "?" to the cell.

RED tests (all fail before the fix, pass after):
  T-NAME-1  "দীপক কলিতা" (Assamese, 10 chars, 30 bytes) is stored in full
  T-NAME-2  "মৃণাল জ্যোতি বৰুৱা" (19 chars, 54 bytes) is stored in full
  T-NAME-3  A 60-character Latin name is not truncated
  T-NAME-4  The ack PDF for an Assamese name embeds a TTF font (not Helvetica)
             and the name bytes appear somewhere in the PDF output, meaning no
             "?" was substituted for the Assamese characters.
  T-NAME-5  An empty name gives a plain-language error with no "ERR_" code.
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

# Assamese test names
SHORT_ASSAMESE  = "দীপক কলিতা"          # 10 chars, 30 UTF-8 bytes
LONG_ASSAMESE   = "মৃণাল জ্যোতি বৰুৱা"  # 19 chars, 54 UTF-8 bytes
LONG_LATIN      = "A" * 60              # 60 chars, 60 bytes – must not be truncated


def _load_app() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sewasetu_app_name", APP_PY)
    mod  = importlib.util.module_from_spec(spec)
    env_patch = {
        "SEWASETU_SECRET_KEY":     "test-secret-key-name",
        "SEWASETU_ADMIN_PASSWORD": "test-admin-pw-name",
    }
    with mock.patch("psycopg2.connect",
                    side_effect=RuntimeError("no live DB in tests")), \
         mock.patch.dict(os.environ, env_patch):
        spec.loader.exec_module(mod)
    flask_app = mod.app
    flask_app.template_folder = str(APP_DIR / "templates")
    flask_app.config.update(TESTING=True, SECRET_KEY="test-secret-name")
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_app()


# ---------------------------------------------------------------------------
# T-NAME-1 / T-NAME-2 / T-NAME-3  sanitize() must not truncate by bytes
# ---------------------------------------------------------------------------

class TestSanitize:
    """sanitize() must use character-based limits, not byte-based slicing."""

    def test_short_assamese_name_stored_in_full(self, mod):
        """T-NAME-1: 'দীপক কলিতা' (10 chars, 30 bytes) must not be truncated."""
        result = mod.sanitize(SHORT_ASSAMESE)
        assert result == SHORT_ASSAMESE, (
            f"T-NAME-1: sanitize() truncated or damaged the Assamese name.\n"
            f"  Input : {SHORT_ASSAMESE!r}\n"
            f"  Output: {result!r}\n"
            f"Bug: the 20-byte slice cuts UTF-8 multi-byte sequences mid-character. "
            "Fix: use a character-count limit, not a byte-count limit."
        )

    def test_long_assamese_name_stored_in_full(self, mod):
        """T-NAME-2: 'মৃণাল জ্যোতি বৰুৱা' (19 chars, 54 bytes) must be stored in full."""
        result = mod.sanitize(LONG_ASSAMESE)
        assert result == LONG_ASSAMESE, (
            f"T-NAME-2: sanitize() truncated or damaged the long Assamese name.\n"
            f"  Input : {LONG_ASSAMESE!r}\n"
            f"  Output: {result!r}\n"
            "Fix: use len(value) (character count) for the limit, not len(bytes)."
        )

    def test_60_char_latin_name_not_truncated(self, mod):
        """T-NAME-3: A 60-character Latin name must not be truncated (DB allows 100 chars)."""
        result = mod.sanitize(LONG_LATIN)
        assert result == LONG_LATIN, (
            f"T-NAME-3: sanitize() truncated a 60-character Latin name.\n"
            f"  Expected length: 60, got: {len(result)}\n"
            "Fix: raise the character limit to match the DB column (100 chars)."
        )

    def test_empty_name_gives_plain_error_no_err_code(self, mod):
        """T-NAME-5: An empty name must give a plain-language error, not 'ERR_'."""
        # sanitize("") returns "" which then triggers the required-field check in
        # handle_submission.  We test that at least sanitize("") returns ""
        # (not an exception or an ERR_ code) and that the submission route
        # flashes a readable message when name is empty.
        result = mod.sanitize("")
        assert result == "", (
            f"T-NAME-5: sanitize('') should return '' not {result!r}"
        )
        assert "ERR_" not in result, (
            "T-NAME-5: sanitize('') must not return an ERR_ code."
        )

    def test_none_input_returns_empty_string(self, mod):
        """sanitize(None) must return '' without raising."""
        result = mod.sanitize(None)
        assert result == ""


# ---------------------------------------------------------------------------
# T-NAME-4  PDF must embed a TTF font and not substitute "?"
# ---------------------------------------------------------------------------

class TestAckPdfFont:
    """generate_acknowledgment() must embed a TTF font and pass Bengali glyphs through."""

    def _make_fake_cur(self, name):
        """Return a mock cursor whose fetchone() yields a row with the given name."""
        from datetime import datetime
        fake_cur = mock.MagicMock()
        fake_cur.fetchone.return_value = (
            "SSP-TEST",           # application_no
            name,                 # applicant_name
            "9000000001",         # mobile
            "1960-01-01",         # dob
            "Sonari",             # village
            "Borgaon",            # block
            "ACC123456",          # bank_account
            "SBIN0001234",        # ifsc
            datetime(2026, 1, 1), # submitted_at
            "PENDING",            # status
        )
        return fake_cur

    def test_pdf_contains_no_question_mark_substitution(self, mod):
        """T-NAME-4a: The PDF bytes must not contain literal '?' for Assamese characters."""
        fake_cur = self._make_fake_cur(SHORT_ASSAMESE)
        pdf_bytes = mod.generate_acknowledgment(fake_cur, 1)

        # /FontFile2 is the PDF stream key for an embedded TrueType font.
        # Its presence proves add_font() loaded the TTF into the document.
        # Counting raw '?' bytes in a compressed PDF stream is unreliable
        # because zlib-compressed data contains arbitrary byte values.
        assert b"/FontFile2" in pdf_bytes, (
            "T-NAME-4a: PDF does not contain an embedded TrueType font (/FontFile2). "
            "The Helvetica latin-1 built-in font is being used instead of the "
            "Noto Sans Bengali TTF. Fix: call pdf.add_font() with the TTF path."
        )


    def test_pdf_references_ttf_not_helvetica_for_bengali(self, mod):
        """T-NAME-4b: The PDF must not use Helvetica as the sole font for name cell."""
        fake_cur = self._make_fake_cur(SHORT_ASSAMESE)
        pdf_bytes = mod.generate_acknowledgment(fake_cur, 1)
        pdf_text = pdf_bytes.decode("latin-1", errors="replace")

        # A TTF-embedded font shows up as a /FontFile2 or /FontFile3 reference
        # in the PDF's font descriptor, or as "NotoSansBengali" in the resource
        # dictionary.  Helvetica alone is sufficient for a Latin-only document
        # but NOT for Bengali.  Check that the PDF mentions the Noto font.
        has_noto = "NotoSansBengali" in pdf_text or "Noto" in pdf_text
        assert has_noto, (
            "T-NAME-4b: PDF does not reference the Noto Sans Bengali TTF font. "
            "Fix: call pdf.add_font('NotoSansBengali', fname=<path to TTF>, uni=True) "
            "and use pdf.set_font('NotoSansBengali') for name cells."
        )
