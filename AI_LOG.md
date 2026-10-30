# AI correction log

## 2026-10-30

- I initially used an incorrect session-state log path in an analysis script because of a typo in the session ID. The analysis was rerun with the correct path before reporting the log findings.
- I initially treated seed-file name rendering as evidence of live database name corruption. This was not sufficient because the file had not been checked through a UTF-8 PostgreSQL client. Live database verification is required before classifying those names as damaged.
- Live PostgreSQL verification showed 0 names containing `?` or U+FFFD; the seed's 300 non-ASCII names are stored as non-ASCII text, so the earlier “mangled” classification was wrong.
- One read-only replacement-character query used invalid `chr(?)` syntax and another shell invocation passed `-` as SQL. Both failed without changing data and were rerun with valid UTF-8 SQL.
