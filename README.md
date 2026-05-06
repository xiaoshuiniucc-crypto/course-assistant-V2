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

## Usability gap review

After comparing `QQ课程AI助手-设计文档.md` and `QQ课程AI助手-模块规格.md` with the current root `src/` implementation, the main gaps to "usable" are:

- `M01`: the adapter could receive real messages, but it still lacked explicit group/private/channel registration APIs and had a real bot startup loop bug.
- `M05` + `M13`: the root implementation was still effectively single-course. `course_id` isolation and `course_groups` mapping from the design docs were missing from the live path.
- `M06`: submissions were stored, but the root flow did not carry course identity through assignment and submission lookup.
- `M07`: grading is robust enough for MVP, but still does not produce structured evidence or course-aware retrieval-backed scoring.
- `M12`: reports are richer now, but they still depend on the simplified storage and grading structures, so traceable per-deduction evidence is still incomplete.

## 2026-05-06 usability branch

Branch: `improve/usability-gap-1`

Completed in this round:

- added minimal `course_id` support to root models for `Courseware`, `Rubric`, `Assignment`, and `HomeworkSubmission`
- upgraded `src/storage/db.py` with:
  - `course_groups` table
  - `course_id` columns and migration guards for existing databases
  - course-aware assignment and submission queries
  - group-to-course binding helpers
  - backward-compatible writes so old tests and temporary objects still work
- upgraded `src/knowledge/knowledge_base.py` so courseware ingestion stores `course_id` metadata and retrieval can accept course-aware filters
- upgraded `src/main.py` so the live root flow derives a `course_id` from group context and uses it in:
  - courseware upload
  - rubric creation
  - assignment creation
  - submission lookup and storage
  - appeal/report/progress/question flows
- upgraded `src/qclaw/adapter.py` with:
  - `on_group_message()`
  - `on_private_message()`
  - `on_channel_message()`
  - `send_private_message()`
  - `send_channel_message()`
  - `get_group_members()`
  - fixed the real `botpy` startup path to avoid nested event loop failure

Verified:

- `py_compile` passed for the updated core files
- `test_mvp.py` passed again with `61 通过 / 0 失败`
- database smoke test confirmed `course_id` storage and group-course binding work

Still to do for real-world usability:

- make `M03` return structured parse output instead of plain text only
- carry `course_id` into deeper grading/report data structures instead of only the root storage path
- add real course-aware search results with source/page metadata instead of `list[str]`
- add explicit teacher/TA course management commands instead of inferring the first course from group naming
- make reports and grading evidence truly traceable to courseware chunks and rubric deductions

### 2026-05-06 evidence pass

Continued on `improve/usability-gap-1`:

- rebuilt `src/knowledge/knowledge_base.py` into a cleaner dual-interface design:
  - keep `search()` for legacy callers
  - add `search_results()` for structured evidence retrieval
- added `SearchResult` to root contracts so retrieval can carry:
  - text
  - score
  - courseware_id
  - chunk_index
  - course_id
  - title
- updated fallback retrieval to include source metadata from `courseware`
- updated `main.py` question answering so student replies now include source-tagged snippets and relevance scores
- updated `report_gen.py` evidence collection so report references come from course-filtered structured retrieval instead of raw text slices

Verified again:

- `py_compile` passed for the updated files
- `test_mvp.py` still passed with `61 通过 / 0 失败`

### 2026-05-06 reply stability hardening

To reduce duplicated or conflicting replies in real QQ conversations, two defensive changes were added in the live root path:

- `src/main.py`
  - added a short-window message dedupe guard before intent routing
  - the same message will only be processed once within the configured TTL window
  - primary key is `message_id`; fallback key uses message type, user, group, normalized text, and file identity
- `src/qclaw/adapter.py`
  - tightened adapter dispatch so a message does not go through both:
    - a type-specific handler such as `on_private_message()`
    - and the generic `on_message()` handler
  - dispatch now prefers the specific handler, and only falls back to the generic handler when no specific handler is registered

Why this was needed:

- some user-visible duplicate replies were consistent with repeated handling of the same incoming event
- even when business routing is correct, duplicate platform delivery or double dispatch in the adapter layer can still hurt UX

Expected effect:

- plain text messages should only trigger one reply path
- file uploads should not produce duplicated command-style and free-chat replies from the same event
- short-term repeated delivery of the same message should be suppressed before routing

### 2026-05-06 AI free-reply integration

The root message flow now supports a real "rule-first, AI-fallback" response path instead of only fixed rule replies plus raw retrieval snippets.

What changed:

- `src/grading/engine.py`
  - added a reusable `answer_question()` method on top of the existing OpenAI-compatible client
  - reused the same configured LLM provider and timeout settings already used for grading
  - when courseware references are available, the prompt asks the model to answer grounded in retrieved content
  - when no references are available, the model can still provide a normal conversational reply
- `src/main.py`
  - `ASK_QUESTION` no longer returns retrieval snippets only
  - it now performs:
    - course-aware retrieval
    - LLM answer generation based on retrieved context
    - retrieval-only fallback if generation fails
  - `UNKNOWN` messages now also try the same AI fallback path before returning the fixed command hint

Behavior after this change:

- rule commands still have highest priority
- course questions can now return natural AI answers instead of only pasted knowledge chunks
- casual text such as greetings or loosely phrased questions can be handled by the LLM fallback path
- if the LLM call fails, the old safe fallback behavior remains available

### 2026-05-06 single-instance protection

The single-instance protection described in `单实例保护变更记录-20260506.md` is consistent with the current code in this workspace and appears to be working as intended.

Code path:

- `src/runtime/single_instance.py`
  - uses `msvcrt.locking` to acquire an exclusive lock file on Windows
  - records current owner metadata and lock history under `run_logs/`
- `src/main.py`
  - acquires the lock before `asyncio.run(main())`
  - exits with code `1` when the lock is already occupied
  - releases the lock on shutdown

Observed runtime evidence:

- `run_logs/assistant.stderr.log` contains `Single-instance lock acquired`
- `run_logs/course_assistant.instance.json` records the live owner process and QQ AppID
- `run_logs/course_assistant.instance.log` contains both:
  - `started`
  - `lock_denied`

Current conclusion:

- within this integrated workspace, the single-instance guard is functioning normally
- it successfully blocks a second startup attempt that targets the same workspace lock file

Related records:

- `单实例保护变更记录-20260506.md`
- `run_logs/course_assistant.instance.json`
- `run_logs/course_assistant.instance.log`
- `run_logs/assistant.stderr.log`

### 2026-05-06 homework and grading hardening

The real QQ submission path was further tightened after live testing exposed two user-facing problems:

- sending only `提交作业` could be treated as a real submission even when no homework body or file content was present
- AI grading could silently fall back to rules, while the active rubric for the course could also drift or be polluted by unrelated text

What changed:

- `src/main.py`
  - tightened homework submission so command text alone is no longer valid submission content
  - submissions now require either:
    - parsed file content
    - or explicit text content after the command
  - duplicate submission checks are enforced before a new submission is created
  - new assignments now bind to the latest rubric for the same `course_id`
  - rubric setup now rejects obvious non-rubric text instead of storing arbitrary follow-up messages as scoring criteria
- `src/homework/receiver.py`
  - submission dedupe now uses course-aware lookup instead of only assignment + student
- `src/grading/engine.py`
  - fixed the project-root `.env` loading path for standalone grading initialization
  - when `ZHIPUAI_API_KEY_FILE` is used and no `OPENAI_BASE_URL` is provided, the engine now defaults to the OpenAI-compatible Zhipu endpoint:
    - `https://open.bigmodel.cn/api/paas/v4/`
  - rule-based fallback grading no longer behaves like a fixed `65` baseline for placeholder-only content
  - unreadable placeholder submissions now receive an explicit non-gradable fallback result instead of a misleading normal score

Observed findings from live data:

- a recent real submission was stored and graded, but it was graded by `rule` because the AI request failed with connection errors
- a recent course rubric had been polluted by unrelated text, which explained incorrect scoring dimensions during fallback grading

Operational note:

- after these fixes, the service should be restarted before running another real QQ test so the updated grading configuration and rubric/assignment binding logic take effect

### 2026-05-06 grading observability

To make real QQ grading easier to inspect and debug, the grading path now records more runtime evidence:

- `src/grading/engine.py`
  - logs the raw AI grading reply into the service log before JSON parsing/recovery
  - writes the final grading result into `run_logs/grading/<submission_id>.json`

The JSON file includes:

- submission id
- assignment id and title
- course id
- student id and name
- grading source (`graded_by`)
- total score
- dimension scores
- feedback
- confidence
- rubric snapshot used for this grading run
- source file path
- submitted/graded timestamps


### 2026-05-06 AI-first grading recovery

Follow-up live QQ testing confirmed that homework grading was already preferring the LLM path, but successful HTTP calls could still degrade into `rule-based fallback` for two reasons:

- the LLM sometimes returned non-JSON grading text even though the request succeeded
- rubric text pasted from QQ could carry Windows-style line breaks and collapse multiple intended dimensions into one parsed dimension

What changed:

- `src/grading/engine.py`
  - grading still prefers AI first
  - after a successful model call, if strict JSON parsing fails, the engine now attempts a text-based score recovery pass before falling back to rules
  - recovered results are stored with an AI grading marker instead of being treated as plain rule grading
- `src/rubric/rubric_parser.py`
  - normalizes `
` / `` to `
` before splitting rubric lines
  - improves the chance that multi-line rubric text pasted from QQ becomes multiple dimensions instead of one merged dimension

Observed live evidence before this fix:

- the bot reached the Zhipu OpenAI-compatible endpoint successfully
- the endpoint returned `HTTP 200 OK`
- grading still fell back because the response was not parseable as strict JSON

Expected effect after restart:

- homework grading should still call AI first
- if the model returns semi-structured scoring text instead of strict JSON, the service now tries to recover dimension scores from that text
- fallback to pure rules should happen less often

### 2026-05-06 AI-generated rubric confirmation flow

The rubric path now supports an explicit teacher approval loop instead of forcing the first rubric version to be fully manual:

- `src/main.py`
  - `布置作业 ...` now first triggers AI rubric draft generation from the assignment title and requirement text
  - the teacher receives a structured draft and can reply `确认评分标准` to finalize it
  - if the teacher replies with modification feedback instead, the assistant regenerates the rubric draft using that feedback
  - the assignment is created only after the rubric is confirmed, so later grading uses the teacher-confirmed rubric
- `src/router/router.py`
  - added `WAITING_RUBRIC_APPROVAL` to keep the teacher inside the multi-turn rubric confirmation flow
- `src/grading/engine.py`
  - added JSON-only AI rubric generation prompts focused on concrete, gradeable dimensions
  - added rubric JSON normalization, dimension validation, and weight normalization
  - added rubric snapshot files under `run_logs/rubrics/*.json` for draft, revised, and confirmed states

Current QQ flow:

1. teacher sends `布置作业 + 作业标题/要求`
2. assistant returns an AI-generated rubric draft
3. teacher either replies `确认评分标准` or sends revision feedback
4. assistant saves the confirmed rubric, creates the assignment, and later grading uses that confirmed rubric
