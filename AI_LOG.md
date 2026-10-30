# AI correction log

## 2026-10-30

- I initially used an incorrect session-state log path in an analysis script because of a typo in the session ID. The analysis was rerun with the correct path before reporting the log findings.
- I initially treated seed-file name rendering as evidence of live database name corruption. This was not sufficient because the file had not been checked through a UTF-8 PostgreSQL client. Live database verification is required before classifying those names as damaged.
- Live PostgreSQL verification showed 0 names containing `?` or U+FFFD; the seed's 300 non-ASCII names are stored as non-ASCII text, so the earlier "mangled" classification was wrong.
- One read-only replacement-character query used invalid `chr(?)` syntax and another shell invocation passed `-` as SQL. Both failed without changing data and were rerun with valid UTF-8 SQL.
- First test commit (6ebbc26) used `pytest.raises(FileNotFoundError)` as a positive assertion, making it green when it should have been red. Corrected in the replacement commit by inverting the test to assert successful import (which then correctly failed).
- T7 (status page rendering) tests error on this host because `fpdf2` is absent and pip can't reach PyPI (network timeout). Added `pytest.importorskip("fpdf")` so the tests skip rather than error on the host; they will be green inside the Docker container where `fpdf2` is installed. Logged here per hard rules.
