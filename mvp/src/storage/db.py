"""
M13 — 数据存储层
SQLite 封装，提供所有表的 CRUD 接口。
MVP 阶段先做同步调用，不做连接池。
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from contracts.models import (
    Rubric, Dimension, HardRule,
    GradingResult, DimensionResult, Deduction,
    AppealRecord, Submission,
)


# ── 数据库文件路径 ──────────────────────────────────────────────
DB_PATH = Path(__file__).parent.parent / "data" / "course_assistant.db"

# ── 建表 SQL ────────────────────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS assignments (
    id         TEXT PRIMARY KEY,
    course_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    deadline   TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rubrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id    TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    version      INTEGER NOT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(assignment_id, version)
);

CREATE TABLE IF NOT EXISTS submissions (
    id             TEXT PRIMARY KEY,
    student_qq     TEXT NOT NULL,
    course_id      TEXT NOT NULL,
    assignment_id  TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    file_type      TEXT,
    submitted_at   TEXT DEFAULT (datetime('now')),
    is_late        INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'submitted',
    score          INTEGER
);

CREATE TABLE IF NOT EXISTS grading_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    dimension_json  TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    grading_context TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (submission_id) REFERENCES submissions(id)
);

CREATE TABLE IF NOT EXISTS appeals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    student_qq      TEXT NOT NULL,
    student_reason  TEXT NOT NULL,
    original_score  INTEGER NOT NULL,
    ai_new_score    INTEGER,
    regrade_detail  TEXT,
    ta_decision     TEXT,
    ta_note         TEXT,
    status          TEXT DEFAULT 'APPEALING',
    created_at      TEXT DEFAULT (datetime('now')),
    decided_at      TEXT,
    FOREIGN KEY (submission_id) REFERENCES submissions(id)
);
"""


# ──  初始化 ────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等，可重复调用）。"""
    conn = _get_conn()
    conn.executescript(CREATE_SQL)
    conn.commit()
    conn.close()


# ──  assignments ───────────────────────────────────────────────

def upsert_assignment(course_id: str, assignment_id: str, title: str,
                      deadline: Optional[str] = None) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO assignments (id, course_id, title, deadline)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET title=excluded.title, deadline=excluded.deadline""",
        (assignment_id, course_id, title, deadline)
    )
    conn.commit()
    conn.close()


# ──  rubrics ───────────────────────────────────────────────────

def save_rubric(rubric: Rubric) -> int:
    """保存评分细则，返回新版本号。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM rubrics WHERE assignment_id = ?",
        (rubric.assignment_id,)
    ).fetchone()
    new_version = row[0] + 1
    rubric.version = new_version
    conn.execute(
        "INSERT INTO rubrics (course_id, assignment_id, content_json, version) VALUES (?, ?, ?, ?)",
        (rubric.course_id, rubric.assignment_id,
         json.dumps(rubric_to_dict(rubric), ensure_ascii=False), new_version)
    )
    conn.commit()
    conn.close()
    return new_version


def get_latest_rubric(assignment_id: str) -> Optional[Rubric]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT content_json FROM rubrics WHERE assignment_id = ? ORDER BY version DESC LIMIT 1",
        (assignment_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return rubric_from_dict(json.loads(row["content_json"]))


# ──  submissions ──────────────────────────────────────────────

def save_submission(sub: Submission) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO submissions
           (id, student_qq, course_id, assignment_id, file_path, file_type,
            submitted_at, is_late, status, score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sub.id, sub.student_qq, sub.course_id, sub.assignment_id,
         sub.file_path, sub.file_type,
         sub.submitted_at.isoformat() if sub.submitted_at else datetime.now().isoformat(),
         1 if sub.is_late else 0, sub.status, sub.score)
    )
    conn.commit()
    conn.close()


def get_submission(sub_id: str) -> Optional[Submission]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
    conn.close()
    return _row_to_submission(row) if row else None


def update_submission_status(sub_id: str, status: str, score: Optional[int] = None) -> None:
    conn = _get_conn()
    if score is not None:
        conn.execute("UPDATE submissions SET status = ?, score = ? WHERE id = ?", (status, score, sub_id))
    else:
        conn.execute("UPDATE submissions SET status = ? WHERE id = ?", (status, sub_id))
    conn.commit()
    conn.close()


# ──  grading_details ──────────────────────────────────────────

def save_grading_result(sub_id: str, result: GradingResult) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO grading_details (submission_id, dimension_json, confidence, grading_context)
           VALUES (?, ?, ?, ?)""",
        (sub_id,
         json.dumps([_dim_result_to_dict(d) for d in result.dimensions], ensure_ascii=False),
         result.confidence, result.grading_context)
    )
    conn.commit()
    conn.close()
    # 同时更新 submissions 表和内存里的 score
    update_submission_status(sub_id, "graded", result.total_score)


# ──  appeals ──────────────────────────────────────────────────

def create_appeal(record: AppealRecord) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO appeals (submission_id, student_qq, student_reason,
           original_score, status)
           VALUES (?, ?, ?, ?, ?)""",
        (record.submission_id, record.student_qq, record.student_reason,
         record.original_score, "APPEALING")
    )
    conn.commit()
    conn.close()
    update_submission_status(record.submission_id, "appealing")
    return cur.lastrowid


def update_appeal_regrade(appeal_id: int, ai_new_score: int, regrade_detail: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE appeals SET ai_new_score = ?, regrade_detail = ?, status = 'UNDER_REVIEW' WHERE id = ?",
        (ai_new_score, regrade_detail, appeal_id)
    )
    conn.commit()
    conn.close()


def resolve_appeal(appeal_id: int, decision: str, note: str, new_score: int) -> None:
    conn = _get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE appeals SET ta_decision = ?, ta_note = ?, status = ?, decided_at = ? WHERE id = ?",
        (decision, note, f"appeal_{decision}".upper(), now, appeal_id)
    )
    conn.commit()
    row = conn.execute("SELECT submission_id FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
    conn.close()
    if row:
        status = "appeal_approved" if decision == "approved" else "appeal_rejected"
        update_submission_status(row["submission_id"], status, new_score if decision == "approved" else None)


def get_appeal(appeal_id: int) -> Optional[AppealRecord]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return AppealRecord(
        id=row["id"],
        submission_id=row["submission_id"],
        student_qq=row["student_qq"],
        student_reason=row["student_reason"],
        original_score=row["original_score"],
        ai_new_score=row["ai_new_score"],
        regrade_detail=row["regrade_detail"],
        ta_decision=row["ta_decision"],
        ta_note=row["ta_note"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
    )


# ── 序列化辅助 ─────────────────────────────────────────────────

def rubric_to_dict(r: Rubric) -> dict:
    return {
        "assignment_id": r.assignment_id,
        "course_id": r.course_id,
        "title": r.title,
        "total_score": r.total_score,
        "deadline": r.deadline.isoformat() if r.deadline else None,
        "dimensions": [{"name": d.name, "max_score": d.max_score, "criteria": d.criteria} for d in r.dimensions],
        "hard_deductions": [{"condition": h.condition, "penalty": h.penalty} for h in r.hard_deductions],
        "version": r.version,
    }


def rubric_from_dict(d: dict) -> Rubric:
    return Rubric(
        assignment_id=d["assignment_id"],
        course_id=d["course_id"],
        title=d["title"],
        total_score=d["total_score"],
        deadline=datetime.fromisoformat(d["deadline"]) if d["deadline"] else None,
        dimensions=[Dimension(name=dim["name"], max_score=dim["max_score"], criteria=dim["criteria"])
                   for dim in d["dimensions"]],
        hard_deductions=[HardRule(condition=h["condition"], penalty=h["penalty"])
                         for h in d.get("hard_deductions", [])],
        version=d["version"],
    )


def _dim_result_to_dict(d: DimensionResult) -> dict:
    return {
        "name": d.name, "score": d.score, "max_score": d.max_score,
        "gain_points": d.gain_points,
        "deductions": [{"point": dd.point, "deduct": dd.deduct,
                        "evidence": dd.evidence, "evidence_source": dd.evidence_source}
                       for dd in d.deductions],
        "dimension_comment": d.dimension_comment,
    }


def _row_to_submission(row: sqlite3.Row) -> Submission:
    return Submission(
        id=row["id"], student_qq=row["student_qq"], course_id=row["course_id"],
        assignment_id=row["assignment_id"], file_path=row["file_path"],
        file_type=row["file_type"],
        submitted_at=datetime.fromisoformat(row["submitted_at"]) if row["submitted_at"] else datetime.now(),
        is_late=bool(row["is_late"]), status=row["status"], score=row["score"],
    )
