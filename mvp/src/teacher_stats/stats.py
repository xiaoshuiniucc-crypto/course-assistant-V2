"""
M10 — 教师统计与预警
向教师推送作业统计信息，以及异常预警。

MVP 策略：
- 统计数据从 SQLite 查询
- 疑似抄袭检测用简单的文本相似度（n-gram overlap）
- 预警推送通过 M01 QclawAdapter
"""

import re
from datetime import datetime
from typing import Optional
from collections import Counter

from contracts.models import ProgressInfo, Alert
from storage import db


class TeacherStats:
    """
    教师统计与预警管理器。
    - get_progress(): 获取作业进度
    - format_progress(): 格式化进度信息
    - check_anomalies(): 检测异常
    - push_progress(): 推送统计
    - push_alert(): 推送预警
    """

    def __init__(self, qclaw_adapter=None):
        self._adapter = qclaw_adapter

    # ── 进度统计 ──────────────────────────────────────────

    def get_progress(self, course_id: str, assignment_id: str,
                     total_students: int = 0) -> Optional[ProgressInfo]:
        """获取指定作业的提交/批改进度。"""
        conn = db._get_conn()

        # 查询作业信息
        assignment = conn.execute(
            "SELECT title, deadline FROM assignments WHERE id = ?",
            (assignment_id,)
        ).fetchone()

        if not assignment:
            conn.close()
            return None

        # 查询提交统计
        submitted = conn.execute(
            "SELECT COUNT(*) as cnt FROM submissions WHERE assignment_id = ? AND course_id = ?",
            (assignment_id, course_id)
        ).fetchone()["cnt"]

        graded = conn.execute(
            "SELECT COUNT(*) as cnt FROM submissions WHERE assignment_id = ? AND course_id = ? AND status = 'graded'",
            (assignment_id, course_id)
        ).fetchone()["cnt"]

        appealing = conn.execute(
            "SELECT COUNT(*) as cnt FROM submissions WHERE assignment_id = ? AND course_id = ? AND status = 'appealing'",
            (assignment_id, course_id)
        ).fetchone()["cnt"]

        # 查询低置信度批改数量
        low_confidence = conn.execute(
            """SELECT COUNT(*) as cnt FROM grading_details gd
               JOIN submissions s ON gd.submission_id = s.id
               WHERE s.assignment_id = ? AND s.course_id = ? AND gd.confidence = 'low'""",
            (assignment_id, course_id)
        ).fetchone()["cnt"]

        conn.close()

        return ProgressInfo(
            course_id=course_id,
            assignment_id=assignment_id,
            assignment_title=assignment["title"],
            deadline=assignment["deadline"],
            total_students=total_students or submitted,
            submitted_count=submitted,
            graded_count=graded,
            pending_count=submitted - graded,
            appealing_count=appealing,
            low_confidence_count=low_confidence,
        )

    def format_progress(self, info: ProgressInfo) -> str:
        """格式化进度信息为可读文本。"""
        submit_rate = (info.submitted_count / info.total_students * 100) if info.total_students > 0 else 0

        lines = [
            f"【交作业进度】{info.assignment_title}",
            f"截止时间：{info.deadline or '未设置'}",
            f"已提交：{info.submitted_count}/{info.total_students}（{submit_rate:.0f}%）",
            f"批改完成：{info.graded_count}/{info.submitted_count}",
            f"待批改：{info.pending_count}份",
            f"申诉中：{info.appealing_count}份",
        ]

        if info.low_confidence_count > 0:
            lines.append(f"⚠️ 低置信度批改：{info.low_confidence_count}份，建议人工复核")

        return "\n".join(lines)

    # ── 异常检测 ──────────────────────────────────────────

    def check_anomalies(self, course_id: str, assignment_id: str) -> list[Alert]:
        """检测异常情况，返回预警列表。"""
        alerts = []

        conn = db._get_conn()

        # 1. 低置信度预警
        low_conf_rows = conn.execute(
            """SELECT s.id, s.student_qq FROM grading_details gd
               JOIN submissions s ON gd.submission_id = s.id
               WHERE s.assignment_id = ? AND s.course_id = ? AND gd.confidence = 'low'""",
            (assignment_id, course_id)
        ).fetchall()

        for row in low_conf_rows:
            alerts.append(Alert(
                alert_type="low_confidence",
                level="info",
                message=f"学生 {row['student_qq']} 的批改置信度为 low，建议人工复核",
                details={"submission_id": row["id"], "student_qq": row["student_qq"]},
            ))

        # 2. 疑似抄袭检测
        plagiarism_alerts = self._check_plagiarism(course_id, assignment_id, conn)
        alerts.extend(plagiarism_alerts)

        # 3. 未提交率预警（截止前24h提交率 < 30%）
        deadline_row = conn.execute(
            "SELECT deadline FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        conn.close()

        if deadline_row and deadline_row["deadline"]:
            try:
                deadline_dt = datetime.fromisoformat(deadline_row["deadline"])
                hours_left = (deadline_dt - datetime.now()).total_seconds() / 3600
                if 0 < hours_left < 24:
                    total_submitted = len(low_conf_rows)  # 简化，实际应单独查询
                    # 这里用 submissions 数量来判断
                    conn2 = db._get_conn()
                    sub_count = conn2.execute(
                        "SELECT COUNT(*) as cnt FROM submissions WHERE assignment_id = ? AND course_id = ?",
                        (assignment_id, course_id)
                    ).fetchone()["cnt"]
                    conn2.close()
                    if sub_count < 3:  # 假设班级人数 > 10，提交 < 3 算低
                        alerts.append(Alert(
                            alert_type="low_submit_rate",
                            level="warning",
                            message=f"距截止仅剩 {hours_left:.0f} 小时，提交数仅 {sub_count} 份",
                            details={"hours_left": hours_left, "submitted": sub_count},
                        ))
            except (ValueError, TypeError):
                pass

        return alerts

    def _check_plagiarism(self, course_id: str, assignment_id: str, conn) -> list[Alert]:
        """
        简单抄袭检测：基于 n-gram overlap 计算文本相似度。
        MVP 阶段只做粗略检测，阈值 > 0.6 标记为疑似抄袭。
        """
        alerts = []

        # 获取所有已提交的作业文本
        rows = conn.execute(
            "SELECT id, student_qq, file_path FROM submissions WHERE assignment_id = ? AND course_id = ?",
            (assignment_id, course_id)
        ).fetchall()

        if len(rows) < 2:
            return alerts

        # 读取作业文本
        from homework.receiver import get_submission_text
        texts = {}
        for row in rows:
            try:
                text = get_submission_text(row["id"])
                if text and len(text) > 100:
                    texts[row["id"]] = {
                        "qq": row["student_qq"],
                        "text": text,
                    }
            except Exception:
                continue

        # 两两比较
        ids = list(texts.keys())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = self._ngram_similarity(texts[ids[i]]["text"], texts[ids[j]]["text"])
                if sim > 0.6:
                    alerts.append(Alert(
                        alert_type="plagiarism",
                        level="warning",
                        message=f"学生 {texts[ids[i]]['qq']} 与 {texts[ids[j]]['qq']} 的作业相似度 {sim:.0%}",
                        details={
                            "student_1": texts[ids[i]]["qq"],
                            "student_2": texts[ids[j]]["qq"],
                            "similarity": sim,
                        },
                    ))

        return alerts

    @staticmethod
    def _ngram_similarity(text1: str, text2: str, n: int = 4) -> float:
        """计算两个文本的 n-gram 重叠率。"""
        def get_ngrams(text, n):
            clean = re.sub(r'\s+', '', text)
            return set(clean[i:i+n] for i in range(len(clean) - n + 1))

        ngrams1 = get_ngrams(text1, n)
        ngrams2 = get_ngrams(text2, n)

        if not ngrams1 or not ngrams2:
            return 0.0

        overlap = len(ngrams1 & ngrams2)
        union = len(ngrams1 | ngrams2)
        return overlap / union if union > 0 else 0.0

    # ── 推送 ──────────────────────────────────────────────

    def push_progress(self, teacher_qq: str, info: ProgressInfo) -> None:
        """推送统计给教师。"""
        msg = self.format_progress(info)
        if self._adapter:
            self._adapter.send_private(teacher_qq, msg)
        else:
            print(msg)

    def push_alert(self, teacher_qq: str, alert: Alert) -> None:
        """推送预警给教师。"""
        icon = "⚠️" if alert.level == "warning" else "ℹ️"
        msg = f"{icon} [{alert.alert_type}] {alert.message}"
        if self._adapter:
            self._adapter.send_private(teacher_qq, msg)
        else:
            print(msg)
