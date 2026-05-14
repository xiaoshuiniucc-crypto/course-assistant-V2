"""
SQLite 存储层
提供所有模块的持久化能力
支持 WAL 模式 + 线程安全写入
"""
from __future__ import annotations
import sqlite3
import json
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path

from contracts.models import (
    Courseware, Rubric, Assignment, HomeworkSubmission,
    GradingResult, Appeal, RubricDimension, SubmitStatus,
    ReportConfig, ReportEvidence, RadarData, ReportType
)


def _now_iso() -> str:
    return datetime.now().isoformat()


class DB:
    """统一数据库访问层（SQLite），线程安全，WAL 模式"""

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS course_groups (
        course_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        group_name TEXT,
        PRIMARY KEY (course_id, group_id)
    );

    CREATE TABLE IF NOT EXISTS courseware (
        id TEXT PRIMARY KEY,
        course_id TEXT NOT NULL DEFAULT 'default',
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        file_path TEXT NOT NULL,
        uploaded_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        meta TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS rubrics (
        id TEXT PRIMARY KEY,
        course_id TEXT NOT NULL DEFAULT 'default',
        title TEXT NOT NULL,
        dimensions TEXT NOT NULL,   -- JSON
        total_score REAL DEFAULT 100.0,
        hard_rules TEXT DEFAULT '[]', -- JSON
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS assignments (
        id TEXT PRIMARY KEY,
        course_id TEXT NOT NULL DEFAULT 'default',
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
        course_id TEXT NOT NULL DEFAULT 'default',
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
        confidence_label TEXT DEFAULT 'medium',
        grading_context TEXT DEFAULT '',
        gain_points TEXT DEFAULT '{}',
        deductions TEXT DEFAULT '{}',
        regrade_diff TEXT DEFAULT '{}',
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
        ai_feedback TEXT DEFAULT '',
        ai_confidence REAL,
        ai_confidence_label TEXT DEFAULT '',
        regrade_result TEXT DEFAULT '{}',
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

    CREATE TABLE IF NOT EXISTS notify_queue (
        id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL,
        group_id TEXT,
        content TEXT NOT NULL,
        priority INTEGER DEFAULT 1,
        status TEXT DEFAULT 'pending',
        retry_count INTEGER DEFAULT 0,
        max_retries INTEGER DEFAULT 3,
        created_at TEXT NOT NULL,
        error_msg TEXT
    );

    CREATE TABLE IF NOT EXISTS ta_actions (
        id TEXT PRIMARY KEY,
        appeal_id TEXT NOT NULL,
        ta_id TEXT NOT NULL,
        action TEXT NOT NULL,
        note TEXT,
        new_score REAL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (appeal_id) REFERENCES appeals(id)
    );

    CREATE INDEX IF NOT EXISTS idx_submissions_assignment
        ON submissions(assignment_id);
    CREATE INDEX IF NOT EXISTS idx_courseware_course
        ON courseware(course_id);
    CREATE INDEX IF NOT EXISTS idx_rubrics_course
        ON rubrics(course_id);
    CREATE INDEX IF NOT EXISTS idx_assignments_course
        ON assignments(course_id);
    CREATE INDEX IF NOT EXISTS idx_submissions_course_assignment
        ON submissions(course_id, assignment_id);
    CREATE INDEX IF NOT EXISTS idx_submissions_student
        ON submissions(student_id);
    CREATE INDEX IF NOT EXISTS idx_appeals_submission
        ON appeals(submission_id);
    CREATE INDEX IF NOT EXISTS idx_ta_actions_appeal
        ON ta_actions(appeal_id);
    CREATE INDEX IF NOT EXISTS idx_ta_actions_ta
        ON ta_actions(ta_id);
    CREATE INDEX IF NOT EXISTS idx_notify_queue_status
        ON notify_queue(status);

    CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY,
        assignment_id TEXT NOT NULL,
        report_type TEXT NOT NULL,
        student_id TEXT,
        file_path TEXT NOT NULL,
        config TEXT NOT NULL,            -- JSON: ReportConfig
        radar_included INTEGER DEFAULT 0,
        evidence_included INTEGER DEFAULT 0,
        generated_at TEXT NOT NULL,
        FOREIGN KEY (assignment_id) REFERENCES assignments(id)
    );

    CREATE TABLE IF NOT EXISTS report_evidences (
        id TEXT PRIMARY KEY,
        report_id TEXT NOT NULL,
        dimension_name TEXT NOT NULL,
        avg_score REAL NOT NULL,
        max_score REAL NOT NULL,
        weakness_level TEXT NOT NULL,
        courseware_refs TEXT DEFAULT '[]',   -- JSON
        improvement_tips TEXT DEFAULT '[]',  -- JSON
        FOREIGN KEY (report_id) REFERENCES reports(id)
    );

    CREATE INDEX IF NOT EXISTS idx_reports_assignment
        ON reports(assignment_id);
    CREATE INDEX IF NOT EXISTS idx_reports_student
        ON reports(student_id);
    CREATE INDEX IF NOT EXISTS idx_report_evidences_report
        ON report_evidences(report_id);
    """

    def __init__(self, db_path: str = "qq_course.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 启用 WAL 模式：读写不互斥，并发性能大幅提升
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 设置合理的 busy 超时（毫秒），避免并发写入时立即报错
        self._conn.execute("PRAGMA busy_timeout=5000")
        # 写入后确保数据落盘
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 线程安全锁：保护所有写操作
        self._write_lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self._write_lock:
            deferred_index_sql = []
            for statement in self.SCHEMA_SQL.split(";"):
                stmt = statement.strip()
                if not stmt:
                    continue
                normalized = stmt.upper()
                if normalized.startswith("CREATE INDEX"):
                    deferred_index_sql.append(stmt + ";")
                    continue
                self._conn.execute(stmt)
            self._migrate_schema()
            for stmt in deferred_index_sql:
                self._conn.execute(stmt)
            self._conn.commit()

    def _migrate_schema(self):
        self._ensure_column("courseware", "course_id", "TEXT NOT NULL DEFAULT 'default'")
        self._ensure_column("rubrics", "course_id", "TEXT NOT NULL DEFAULT 'default'")
        self._ensure_column("assignments", "course_id", "TEXT NOT NULL DEFAULT 'default'")
        self._ensure_column("submissions", "course_id", "TEXT NOT NULL DEFAULT 'default'")
        self._ensure_column("grading_results", "confidence_label", "TEXT DEFAULT 'medium'")
        self._ensure_column("grading_results", "grading_context", "TEXT DEFAULT ''")
        self._ensure_column("grading_results", "gain_points", "TEXT DEFAULT '{}'")
        self._ensure_column("grading_results", "deductions", "TEXT DEFAULT '{}'")
        self._ensure_column("grading_results", "regrade_diff", "TEXT DEFAULT '{}'")
        self._ensure_column("appeals", "ai_feedback", "TEXT DEFAULT ''")
        self._ensure_column("appeals", "ai_confidence", "REAL")
        self._ensure_column("appeals", "ai_confidence_label", "TEXT DEFAULT ''")
        self._ensure_column("appeals", "regrade_result", "TEXT DEFAULT '{}'")

    def _ensure_column(self, table_name: str, column_name: str, column_def: str):
        columns = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(column["name"] == column_name for column in columns):
            return
        self._conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"
        )

    def close(self):
        """关闭数据库连接"""
        try:
            self._conn.close()
        except Exception:
            pass

    def _now(self) -> str:
        return _now_iso()

    def _execute_write(self, sql: str, params: tuple = ()):
        """线程安全的写操作：加锁执行 SQL 并提交"""
        with self._write_lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _execute_write_many(self, operations: list):
        """线程安全的批量写操作

        Args:
            operations: [(sql, params), ...] 列表，在一个锁内顺序执行并提交
        """
        with self._write_lock:
            for sql, params in operations:
                self._conn.execute(sql, params)
            self._conn.commit()

    # ── Courseware ──────────────────────────────

    def save_courseware(self, cw: Courseware):
        course_id = getattr(cw, "course_id", "default") or "default"
        self._execute_write(
            "INSERT OR REPLACE INTO courseware"
            "(id,course_id,title,content,file_path,uploaded_by,created_at,meta)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (cw.id, course_id, cw.title, cw.content, cw.file_path,
             cw.uploaded_by, cw.created_at.isoformat(),
             json.dumps(cw.meta, ensure_ascii=False))
        )

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
            course_id=row["course_id"] or "default",
            meta=json.loads(row["meta"])
        )

    # ── Rubric ──────────────────────────────────

    def save_rubric(self, r: Rubric):
        course_id = getattr(r, "course_id", "default") or "default"
        dims = json.dumps(
            [{"name": d.name, "weight": d.weight,
              "description": d.description, "max_score": d.max_score}
             for d in r.dimensions],
            ensure_ascii=False
        )
        self._execute_write(
            "INSERT OR REPLACE INTO rubrics"
            "(id,course_id,title,dimensions,total_score,hard_rules,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (r.id, course_id, r.title, dims, r.total_score,
             json.dumps(r.hard_rules, ensure_ascii=False),
             r.created_at.isoformat())
        )

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
            course_id=row["course_id"] or "default",
            total_score=row["total_score"],
            hard_rules=json.loads(row["hard_rules"]),
            created_at=datetime.fromisoformat(row["created_at"])
        )

    # ── Assignment ─────────────────────────────

    def save_assignment(self, a: Assignment):
        course_id = getattr(a, "course_id", "default") or "default"
        self._execute_write(
            "INSERT OR REPLACE INTO assignments"
            "(id,course_id,title,description,rubric_id,courseware_id,"
            "deadline,max_score,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (a.id, course_id, a.title, a.description, a.rubric_id,
             a.courseware_id,
             a.deadline.isoformat() if a.deadline else None,
             a.max_score, a.created_at.isoformat())
        )

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
            course_id=row["course_id"] or "default",
            max_score=row["max_score"],
            created_at=datetime.fromisoformat(row["created_at"])
        )

    def list_assignments(self, course_id: Optional[str] = None) -> List[Assignment]:
        if course_id:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE course_id=? ORDER BY created_at",
                (course_id,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM assignments ORDER BY created_at").fetchall()
        return [self.get_assignment(r["id"]) for r in rows]   # type: ignore

    # ── Submission ──────────────────────────────

    def save_submission(self, s: HomeworkSubmission):
        course_id = getattr(s, "course_id", "default") or "default"
        self._execute_write(
            "INSERT OR REPLACE INTO submissions"
            "(id,course_id,assignment_id,student_id,student_name,content,"
            "file_path,status,submitted_at,score,feedback,"
            "graded_at,plagiarized,appeal_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.id, course_id, s.assignment_id, s.student_id, s.student_name,
             s.content, s.file_path, s.status,
             s.submitted_at.isoformat(),
             s.score, s.feedback,
             s.graded_at.isoformat() if s.graded_at else None,
             1 if s.plagiarized else 0, s.appeal_status)
        )

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
            course_id=row["course_id"] or "default",
            status=row["status"],
            submitted_at=datetime.fromisoformat(row["submitted_at"]),
            score=row["score"], feedback=row["feedback"],
            graded_at=(datetime.fromisoformat(row["graded_at"])
                       if row["graded_at"] else None),
            plagiarized=bool(row["plagiarized"]),
            appeal_status=row["appeal_status"]
        )

    def list_submissions(
        self,
        assignment_id: str,
        course_id: Optional[str] = None,
    ) -> List[HomeworkSubmission]:
        if course_id:
            rows = self._conn.execute(
                "SELECT * FROM submissions WHERE assignment_id=? AND course_id=?",
                (assignment_id, course_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM submissions WHERE assignment_id=?",
                (assignment_id,)
            ).fetchall()
        return [self.get_submission(r["id"]) for r in rows]   # type: ignore

    def get_student_submission(
        self,
        assignment_id: str,
        student_id: str,
        course_id: Optional[str] = None,
    ) -> Optional[HomeworkSubmission]:
        if course_id:
            row = self._conn.execute(
                "SELECT * FROM submissions "
                "WHERE assignment_id=? AND student_id=? AND course_id=?",
                (assignment_id, student_id, course_id)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM submissions "
                "WHERE assignment_id=? AND student_id=?",
                (assignment_id, student_id)
            ).fetchone()
        return self.get_submission(row["id"]) if row else None

    # ── Grading Result ─────────────────────────

    def save_grading_result(self, g: GradingResult):
        self._execute_write(
            "INSERT OR REPLACE INTO grading_results"
            "(submission_id,dimension_scores,total_score,feedback,"
            "confidence,confidence_label,grading_context,gain_points,"
            "deductions,regrade_diff,graded_by,graded_at,appeal_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.submission_id,
             json.dumps(g.dimension_scores, ensure_ascii=False),
             g.total_score, g.feedback, g.confidence,
             g.confidence_label,
             g.grading_context,
             json.dumps(g.gain_points, ensure_ascii=False),
             json.dumps(g.deductions, ensure_ascii=False),
             json.dumps(g.regrade_diff, ensure_ascii=False),
             g.graded_by, g.graded_at.isoformat(), g.appeal_count)
        )

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
            confidence_label=row["confidence_label"] or "medium",
            grading_context=row["grading_context"] or "",
            gain_points=json.loads(row["gain_points"] or "{}"),
            deductions=json.loads(row["deductions"] or "{}"),
            regrade_diff=json.loads(row["regrade_diff"] or "{}"),
            graded_by=row["graded_by"],
            graded_at=datetime.fromisoformat(row["graded_at"]),
            appeal_count=row["appeal_count"]
        )

    # ── Appeal ──────────────────────────────────

    def save_appeal(self, a: Appeal):
        self._execute_write_many([
            (
                "INSERT OR REPLACE INTO appeals"
                "(id,submission_id,student_id,reason,created_at,"
                "status,new_score,reviewed_by,ai_feedback,"
                "ai_confidence,ai_confidence_label,regrade_result)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.submission_id, a.student_id, a.reason,
                 a.created_at.isoformat(), a.status,
                 a.new_score, a.reviewed_by, a.ai_feedback,
                 a.ai_confidence, a.ai_confidence_label,
                 json.dumps(a.regrade_result, ensure_ascii=False))
            ),
            (
                "UPDATE submissions SET appeal_status=? WHERE id=?",
                (a.status, a.submission_id)
            ),
        ])

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
            reviewed_by=row["reviewed_by"],
            ai_feedback=row["ai_feedback"] or "",
            ai_confidence=row["ai_confidence"],
            ai_confidence_label=row["ai_confidence_label"] or "",
            regrade_result=json.loads(row["regrade_result"] or "{}")
        )

    def list_pending_appeals(self) -> List[Appeal]:
        rows = self._conn.execute(
            "SELECT * FROM appeals WHERE status='pending'"
        ).fetchall()
        return [self.get_appeal(r["id"]) for r in rows]   # type: ignore

    # ── Knowledge Chunks ───────────────────────

    def save_chunks(self, courseware_id: str,
                    chunks: List[str], embeddings: List = None,
                    course_id: str = "default"):
        with self._write_lock:
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
                    (f"{course_id}_{courseware_id}_{i}", courseware_id, i, text, emb)
                )
            self._conn.commit()

    def search_chunks(self, query: str, top_k: int = 3,
                      courseware_id: Optional[str] = None) -> List[str]:
        """关键词 fallback 搜索（chromadb 不可用时使用）"""
        if courseware_id:
            all_rows = self._conn.execute(
                "SELECT text FROM knowledge_chunks WHERE courseware_id=?",
                (courseware_id,)
            ).fetchall()
        else:
            all_rows = self._conn.execute(
                "SELECT text FROM knowledge_chunks"
            ).fetchall()
        results = []
        query_lower = query.lower()
        for row in all_rows:
            if query_lower in row["text"].lower():
                results.append(row["text"])
        return results[:top_k]

    def bind_group_to_course(self, course_id: str, group_id: str, group_name: str = ""):
        self._execute_write(
            "INSERT OR REPLACE INTO course_groups(course_id,group_id,group_name)"
            " VALUES (?,?,?)",
            (course_id, group_id, group_name),
        )

    def get_course_id_by_group(self, group_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT course_id FROM course_groups WHERE group_id=?",
            (group_id,),
        ).fetchone()
        return row["course_id"] if row else None

    # ── Session (多轮对话) ─────────────────────

    def save_session(self, session_id: str, user_id: str,
                     intent: str = None, waiting_for: str = None,
                     slots: Dict = None):
        with self._write_lock:
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

    # ── Reports ─────────────────────────────

    def save_report(self, report_id: str, assignment_id: str,
                    report_type: str, file_path: str,
                    config: ReportConfig, student_id: str = None,
                    radar_included: bool = False,
                    evidence_included: bool = False):
        """保存报告记录"""
        self._execute_write(
            "INSERT OR REPLACE INTO reports"
            "(id,assignment_id,report_type,student_id,file_path,"
            "config,radar_included,evidence_included,generated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, assignment_id, report_type, student_id, file_path,
             json.dumps({
                 "assignment_id": config.assignment_id,
                 "report_type": config.report_type,
                 "student_id": config.student_id,
                 "include_radar": config.include_radar,
                 "include_evidence": config.include_evidence,
                 "include_ranking": config.include_ranking,
                 "include_suggestions": config.include_suggestions,
                 "comparison_students": config.comparison_students,
             }, ensure_ascii=False),
             1 if radar_included else 0,
             1 if evidence_included else 0,
             self._now())
        )

    def get_report(self, report_id: str) -> Optional[Dict]:
        """获取报告记录"""
        row = self._conn.execute(
            "SELECT * FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "assignment_id": row["assignment_id"],
            "report_type": row["report_type"],
            "student_id": row["student_id"],
            "file_path": row["file_path"],
            "config": json.loads(row["config"]),
            "radar_included": bool(row["radar_included"]),
            "evidence_included": bool(row["evidence_included"]),
            "generated_at": row["generated_at"],
        }

    def list_reports(self, assignment_id: str = None,
                     student_id: str = None) -> List[Dict]:
        """列出报告记录"""
        conditions = []
        params = []
        if assignment_id:
            conditions.append("assignment_id=?")
            params.append(assignment_id)
        if student_id:
            conditions.append("student_id=?")
            params.append(student_id)
        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM reports WHERE {where} ORDER BY generated_at DESC",
            tuple(params)
        ).fetchall()
        return [self.get_report(r["id"]) for r in rows]

    # ── Report Evidences ──────────────────────

    def save_report_evidence(self, report_id: str,
                             evidence: ReportEvidence):
        """保存课件依据条目"""
        eid = f"ev_{evidence.dimension_name}_{report_id}"
        self._execute_write(
            "INSERT OR REPLACE INTO report_evidences"
            "(id,report_id,dimension_name,avg_score,max_score,"
            "weakness_level,courseware_refs,improvement_tips)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (eid, report_id, evidence.dimension_name,
             evidence.avg_score, evidence.max_score,
             evidence.weakness_level,
             json.dumps(evidence.courseware_refs, ensure_ascii=False),
             json.dumps(evidence.improvement_tips, ensure_ascii=False))
        )

    def get_report_evidences(self, report_id: str) -> List[ReportEvidence]:
        """获取报告的课件依据列表"""
        rows = self._conn.execute(
            "SELECT * FROM report_evidences WHERE report_id=?",
            (report_id,)
        ).fetchall()
        return [
            ReportEvidence(
                dimension_name=row["dimension_name"],
                avg_score=row["avg_score"],
                max_score=row["max_score"],
                weakness_level=row["weakness_level"],
                courseware_refs=json.loads(row["courseware_refs"]),
                improvement_tips=json.loads(row["improvement_tips"]),
            )
            for row in rows
        ]

    # ── 维度统计 ──────────────────────────────

    def get_dimension_stats(self, assignment_id: str) -> Dict[str, Dict]:
        """获取各维度的统计数据（平均分、最高分、最低分）"""
        submissions = self.list_submissions(assignment_id)
        dim_stats: Dict[str, Dict] = {}

        for sub in submissions:
            result = self.get_grading_result(sub.id)
            if not result or not result.dimension_scores:
                continue
            for dim_name, score in result.dimension_scores.items():
                if dim_name not in dim_stats:
                    dim_stats[dim_name] = {"scores": [], "count": 0}
                dim_stats[dim_name]["scores"].append(score)
                dim_stats[dim_name]["count"] += 1

        # 计算统计量
        result = {}
        for dim_name, data in dim_stats.items():
            scores = data["scores"]
            result[dim_name] = {
                "avg": round(sum(scores) / len(scores), 2) if scores else 0,
                "max": max(scores) if scores else 0,
                "min": min(scores) if scores else 0,
                "count": len(scores),
            }
        return result
