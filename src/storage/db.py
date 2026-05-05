"""
SQLite 存储层
提供所有模块的持久化能力
"""
from __future__ import annotations
import sqlite3
import json
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path

from contracts.models import (
    Courseware, Rubric, Assignment, HomeworkSubmission,
    GradingResult, Appeal, RubricDimension, SubmitStatus
)


def _now_iso() -> str:
    return datetime.now().isoformat()


class DB:
    """统一数据库访问层（SQLite）"""

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS courseware (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        file_path TEXT NOT NULL,
        uploaded_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        meta TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS rubrics (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        dimensions TEXT NOT NULL,   -- JSON
        total_score REAL DEFAULT 100.0,
        hard_rules TEXT DEFAULT '[]', -- JSON
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS assignments (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        rubric_id TEXT,
        courseware_id TEXT,
        deadline TEXT,
        max_score REAL DEFAULT 100.0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (rubric_id) REFERENCES rubrics(id),
        FOREIGN KEY (courseware_id) REFERENCES courseware(id)
    );

    CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY,
        assignment_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        student_name TEXT NOT NULL,
        content TEXT NOT NULL,
        file_path TEXT,
        status TEXT DEFAULT 'pending',
        submitted_at TEXT NOT NULL,
        score REAL,
        feedback TEXT,
        graded_at TEXT,
        plagiarized INTEGER DEFAULT 0,
        appeal_status TEXT DEFAULT 'none',
        FOREIGN KEY (assignment_id) REFERENCES assignments(id)
    );

    CREATE TABLE IF NOT EXISTS grading_results (
        submission_id TEXT PRIMARY KEY,
        dimension_scores TEXT NOT NULL,  -- JSON
        total_score REAL NOT NULL,
        feedback TEXT NOT NULL,
        confidence REAL NOT NULL,
        graded_by TEXT DEFAULT 'ai',
        graded_at TEXT NOT NULL,
        appeal_count INTEGER DEFAULT 0,
        FOREIGN KEY (submission_id) REFERENCES submissions(id)
    );

    CREATE TABLE IF NOT EXISTS appeals (
        id TEXT PRIMARY KEY,
        submission_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        new_score REAL,
        reviewed_by TEXT,
        FOREIGN KEY (submission_id) REFERENCES submissions(id)
    );

    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id TEXT PRIMARY KEY,
        courseware_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding TEXT,            -- JSON，可选
        FOREIGN KEY (courseware_id) REFERENCES courseware(id)
    );

    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        intent TEXT,
        waiting_for TEXT,
        slots TEXT DEFAULT '{}',  -- JSON
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_submissions_assignment
        ON submissions(assignment_id);
    CREATE INDEX IF NOT EXISTS idx_submissions_student
        ON submissions(student_id);
    CREATE INDEX IF NOT EXISTS idx_appeals_submission
        ON appeals(submission_id);
    """

    def __init__(self, db_path: str = "qq_course.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript(self.SCHEMA_SQL)
        self._conn.commit()

    def _now(self) -> str:
        return _now_iso()

    # ── Courseware ──────────────────────────────

    def save_courseware(self, cw: Courseware):
        self._conn.execute(
            "INSERT OR REPLACE INTO courseware"
            "(id,title,content,file_path,uploaded_by,created_at,meta)"
            " VALUES (?,?,?,?,?,?,?)",
            (cw.id, cw.title, cw.content, cw.file_path,
             cw.uploaded_by, cw.created_at.isoformat(),
             json.dumps(cw.meta, ensure_ascii=False))
        )
        self._conn.commit()

    def get_courseware(self, cw_id: str) -> Optional[Courseware]:
        row = self._conn.execute(
            "SELECT * FROM courseware WHERE id=?", (cw_id,)
        ).fetchone()
        if not row:
            return None
        return Courseware(
            id=row["id"], title=row["title"], content=row["content"],
            file_path=row["file_path"], uploaded_by=row["uploaded_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            meta=json.loads(row["meta"])
        )

    # ── Rubric ──────────────────────────────────

    def save_rubric(self, r: Rubric):
        dims = json.dumps(
            [{"name": d.name, "weight": d.weight,
              "description": d.description, "max_score": d.max_score}
             for d in r.dimensions],
            ensure_ascii=False
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO rubrics"
            "(id,title,dimensions,total_score,hard_rules,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (r.id, r.title, dims, r.total_score,
             json.dumps(r.hard_rules, ensure_ascii=False),
             r.created_at.isoformat())
        )
        self._conn.commit()

    def get_rubric(self, rid: str) -> Optional[Rubric]:
        row = self._conn.execute(
            "SELECT * FROM rubrics WHERE id=?", (rid,)
        ).fetchone()
        if not row:
            return None
        dims = [
            RubricDimension(
                name=d["name"], weight=d["weight"],
                description=d.get("description", ""),
                max_score=d.get("max_score", 100.0)
            ) for d in json.loads(row["dimensions"])
        ]
        return Rubric(
            id=row["id"], title=row["title"], dimensions=dims,
            total_score=row["total_score"],
            hard_rules=json.loads(row["hard_rules"]),
            created_at=datetime.fromisoformat(row["created_at"])
        )

    # ── Assignment ─────────────────────────────

    def save_assignment(self, a: Assignment):
        self._conn.execute(
            "INSERT OR REPLACE INTO assignments"
            "(id,title,description,rubric_id,courseware_id,"
            "deadline,max_score,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (a.id, a.title, a.description, a.rubric_id,
             a.courseware_id,
             a.deadline.isoformat() if a.deadline else None,
             a.max_score, a.created_at.isoformat())
        )
        self._conn.commit()

    def get_assignment(self, aid: str) -> Optional[Assignment]:
        row = self._conn.execute(
            "SELECT * FROM assignments WHERE id=?", (aid,)
        ).fetchone()
        if not row:
            return None
        return Assignment(
            id=row["id"], title=row["title"],
            description=row["description"],
            rubric_id=row["rubric_id"],
            courseware_id=row["courseware_id"],
            deadline=(datetime.fromisoformat(row["deadline"])
                      if row["deadline"] else None),
            max_score=row["max_score"],
            created_at=datetime.fromisoformat(row["created_at"])
        )

    def list_assignments(self) -> List[Assignment]:
        rows = self._conn.execute("SELECT * FROM assignments").fetchall()
        return [self.get_assignment(r["id"]) for r in rows]   # type: ignore

    # ── Submission ──────────────────────────────

    def save_submission(self, s: HomeworkSubmission):
        self._conn.execute(
            "INSERT OR REPLACE INTO submissions"
            "(id,assignment_id,student_id,student_name,content,"
            "file_path,status,submitted_at,score,feedback,"
            "graded_at,plagiarized,appeal_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.id, s.assignment_id, s.student_id, s.student_name,
             s.content, s.file_path, s.status,
             s.submitted_at.isoformat(),
             s.score, s.feedback,
             s.graded_at.isoformat() if s.graded_at else None,
             1 if s.plagiarized else 0, s.appeal_status)
        )
        self._conn.commit()

    def get_submission(self, sid: str) -> Optional[HomeworkSubmission]:
        row = self._conn.execute(
            "SELECT * FROM submissions WHERE id=?", (sid,)
        ).fetchone()
        if not row:
            return None
        return HomeworkSubmission(
            id=row["id"], assignment_id=row["assignment_id"],
            student_id=row["student_id"],
            student_name=row["student_name"],
            content=row["content"], file_path=row["file_path"],
            status=row["status"],
            submitted_at=datetime.fromisoformat(row["submitted_at"]),
            score=row["score"], feedback=row["feedback"],
            graded_at=(datetime.fromisoformat(row["graded_at"])
                       if row["graded_at"] else None),
            plagiarized=bool(row["plagiarized"]),
            appeal_status=row["appeal_status"]
        )

    def list_submissions(self, assignment_id: str) -> List[HomeworkSubmission]:
        rows = self._conn.execute(
            "SELECT * FROM submissions WHERE assignment_id=?",
            (assignment_id,)
        ).fetchall()
        return [self.get_submission(r["id"]) for r in rows]   # type: ignore

    def get_student_submission(
        self, assignment_id: str, student_id: str
    ) -> Optional[HomeworkSubmission]:
        row = self._conn.execute(
            "SELECT * FROM submissions "
            "WHERE assignment_id=? AND student_id=?",
            (assignment_id, student_id)
        ).fetchone()
        return self.get_submission(row["id"]) if row else None

    # ── Grading Result ─────────────────────────

    def save_grading_result(self, g: GradingResult):
        self._conn.execute(
            "INSERT OR REPLACE INTO grading_results"
            "(submission_id,dimension_scores,total_score,feedback,"
            "confidence,graded_by,graded_at,appeal_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (g.submission_id,
             json.dumps(g.dimension_scores, ensure_ascii=False),
             g.total_score, g.feedback, g.confidence,
             g.graded_by, g.graded_at.isoformat(), g.appeal_count)
        )
        self._conn.commit()

    def get_grading_result(self, submission_id: str) -> Optional[GradingResult]:
        row = self._conn.execute(
            "SELECT * FROM grading_results WHERE submission_id=?",
            (submission_id,)
        ).fetchone()
        if not row:
            return None
        return GradingResult(
            submission_id=row["submission_id"],
            dimension_scores=json.loads(row["dimension_scores"]),
            total_score=row["total_score"],
            feedback=row["feedback"],
            confidence=row["confidence"],
            graded_by=row["graded_by"],
            graded_at=datetime.fromisoformat(row["graded_at"]),
            appeal_count=row["appeal_count"]
        )

    # ── Appeal ──────────────────────────────────

    def save_appeal(self, a: Appeal):
        self._conn.execute(
            "INSERT OR REPLACE INTO appeals"
            "(id,submission_id,student_id,reason,created_at,"
            "status,new_score,reviewed_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (a.id, a.submission_id, a.student_id, a.reason,
             a.created_at.isoformat(), a.status,
             a.new_score, a.reviewed_by)
        )
        # 同时更新 submissions 表中 appeal_status
        self._conn.execute(
            "UPDATE submissions SET appeal_status=? WHERE id=?",
            (a.status, a.submission_id)
        )
        self._conn.commit()

    def get_appeal(self, aid: str) -> Optional[Appeal]:
        row = self._conn.execute(
            "SELECT * FROM appeals WHERE id=?", (aid,)
        ).fetchone()
        if not row:
            return None
        return Appeal(
            id=row["id"], submission_id=row["submission_id"],
            student_id=row["student_id"], reason=row["reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            status=row["status"], new_score=row["new_score"],
            reviewed_by=row["reviewed_by"]
        )

    def list_pending_appeals(self) -> List[Appeal]:
        rows = self._conn.execute(
            "SELECT * FROM appeals WHERE status='pending'"
        ).fetchall()
        return [self.get_appeal(r["id"]) for r in rows]   # type: ignore

    # ── Knowledge Chunks ───────────────────────

    def save_chunks(self, courseware_id: str,
                    chunks: List[str], embeddings: List = None):
        # 先删除旧分块
        self._conn.execute(
            "DELETE FROM knowledge_chunks WHERE courseware_id=?",
            (courseware_id,)
        )
        for i, text in enumerate(chunks):
            emb = None
            if embeddings and i < len(embeddings):
                emb = json.dumps(embeddings[i], ensure_ascii=False)
            self._conn.execute(
                "INSERT INTO knowledge_chunks"
                "(id,courseware_id,chunk_index,text,embedding)"
                " VALUES (?,?,?,?,?)",
                (f"{courseware_id}_{i}", courseware_id, i, text, emb)
            )
        self._conn.commit()

    def search_chunks(self, query: str, top_k: int = 3) -> List[str]:
        """关键词 fallback 搜索（chromadb 不可用时使用）"""
        all_rows = self._conn.execute(
            "SELECT text FROM knowledge_chunks"
        ).fetchall()
        results = []
        query_lower = query.lower()
        for row in all_rows:
            if query_lower in row["text"].lower():
                results.append(row["text"])
        return results[:top_k]

    # ── Session (多轮对话) ─────────────────────

    def save_session(self, session_id: str, user_id: str,
                     intent: str = None, waiting_for: str = None,
                     slots: Dict = None):
        existing = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE sessions SET intent=?, waiting_for=?, "
                "slots=?, updated_at=? WHERE session_id=?",
                (intent, waiting_for,
                 json.dumps(slots or {}, ensure_ascii=False),
                 self._now(), session_id)
            )
        else:
            self._conn.execute(
                "INSERT INTO sessions"
                "(session_id,user_id,intent,waiting_for,slots,updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, user_id, intent, waiting_for,
                 json.dumps(slots or {}, ensure_ascii=False),
                 self._now())
            )
        self._conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "intent": row["intent"],
            "waiting_for": row["waiting_for"],
            "slots": json.loads(row["slots"]),
            "updated_at": row["updated_at"]
        }

    # ── 统计数据 ─────────────────────────────

    def get_submission_stats(self, assignment_id: str) -> Dict:
        """返回作业提交统计：总分、平均分、提交率等"""
        rows = self._conn.execute(
            "SELECT score FROM submissions "
            "WHERE assignment_id=? AND status='graded'",
            (assignment_id,)
        ).fetchall()
        scores = [r["score"] for r in rows if r["score"] is not None]
        total = len(scores)
        avg = sum(scores) / total if total > 0 else 0
        submit_count = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM submissions "
            "WHERE assignment_id=?", (assignment_id,)
        ).fetchone()["cnt"]
        return {
            "total_students": total,
            "submitted": submit_count,
            "avg_score": round(avg, 2),
            "max_score": max(scores) if scores else 0,
            "min_score": min(scores) if scores else 0,
        }
