# course-assistant-V2 integrated workspace

This workspace is based on the latest upstream code and is used to re-apply validated local fixes without disturbing the original local branch.

## Base

- upstream source: `course-assistant-V2-upstream-20260505-233549`
- integration branch: `integrate/local-fixes`

## Migrated fixes

### 2026-05-05

- restored attachment passthrough in `M01`
- fixed duplicated QQ intents setup in `M01`
- restored local/mock role inference fallback in `M01`
- restored mock `file_url` / `file_name` support for upload testing
- repaired courseware upload flow in `main.py` to match `FileParser.parse() -> str`
- restored `M11` 48-hour appeal window and "must be graded first" guard
- fixed async student notification scheduling after appeal review
- tightened `M02` question intent routing
- restored robust `M07` JSON extraction, dimension matching, and `appeal_count` handling
- removed the `parser` package name collision by introducing `src/fileparser`
- made `python-dotenv` optional at runtime so smoke runs do not fail hard without it
- made `test_mvp.py` UTF-8 friendly on Windows consoles

## Next

- run smoke tests for upload, grading, appeal, TA review
- compare `report_gen.py` and `ta_channel.py` against `plan/` before more feature work
