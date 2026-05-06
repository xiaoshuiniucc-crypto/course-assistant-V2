# AGENTS.md

This integration workspace follows three rules:

1. Keep upstream additions unless they are clearly broken.
2. Re-apply only validated local fixes.
3. Record every integration step in `README.md`.

## Current focus

### P0

- `M01` adapter reliability and mock parity
- `main.py` courseware upload chain
- `M11` appeal window and approval notification
- `M05` / `M13` course-aware root path (`course_id` + `course_groups`)

### P1

- `M02` question intent false positives
- `M07` grading parser robustness
- evidence-backed grading/report alignment with `plan/`

## Working style

- prefer small, targeted merges over whole-file rollback
- re-check syntax after each integration batch
- if upstream already has a better implementation, keep upstream
- when adding new root features, keep backward compatibility with existing `test_mvp.py`
