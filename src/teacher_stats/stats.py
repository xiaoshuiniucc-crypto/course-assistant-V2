"""
教师统计模块
学习进度统计 + 异常检测（低置信度/抄袭/低提交率）
"""
from __future__ import annotations
from typing import List, Dict, Optional
from collections import Counter

from contracts.models import TeacherAlert, StudentProgress, SubmitStatus
from storage.db import DB


class TeacherStats:
    """教师统计与预警"""

    def __init__(self, db: DB):
        self.db = db

    def get_assignment_progress(self, assignment_id: str) -> Dict:
        """获取作业进度统计"""
        submissions = self.db.list_submissions(assignment_id)

        total = len(submissions)
        graded = [s for s in submissions if s.status == SubmitStatus.GRADED]
        pending = [s for s in submissions if s.status == SubmitStatus.PENDING]
        appealed = [s for s in submissions if s.appeal_status == "pending"]

        scores = [s.score for s in graded if s.score is not None]
        avg = sum(scores) / len(scores) if scores else 0

        return {
            "assignment_id": assignment_id,
            "total_submissions": total,
            "graded_count": len(graded),
            "pending_count": len(pending),
            "appeal_count": len(appealed),
            "avg_score": round(avg, 2),
            "max_score": max(scores) if scores else 0,
            "min_score": min(scores) if scores else 0,
        }

    def get_student_progress(self, assignment_id: str) -> List[StudentProgress]:
        """获取学生进度列表"""
        submissions = self.db.list_submissions(assignment_id)
        scores = [s.score for s in submissions if s.score is not None]

        progresses = []
        for s in submissions:
            # 排名百分位
            rank_pct = None
            if s.score is not None and scores:
                below = sum(1 for sc in scores if sc <= s.score)
                rank_pct = round(below / len(scores) * 100, 1)

            progresses.append(StudentProgress(
                student_id=s.student_id,
                assignment_id=assignment_id,
                submitted=s.status != SubmitStatus.PENDING,
                score=s.score,
                rank_percentile=rank_pct,
            ))

        return progresses

    # ── 异常检测 ────────────────────────────

    def detect_anomalies(self, assignment_id: str) -> List[TeacherAlert]:
        """检测异常: 低置信度 / 抄袭 / 低提交率"""
        alerts: List[TeacherAlert] = []
        submissions = self.db.list_submissions(assignment_id)

        if not submissions:
            return alerts

        # 1. 低置信度预警
        for s in submissions:
            result = self.db.get_grading_result(s.id)
            if result and result.confidence < 0.5:
                alerts.append(TeacherAlert(
                    alert_type="low_confidence",
                    severity="warning",
                    message=f"学生 {s.student_name} 的批改置信度较低 ({result.confidence:.2f})",
                    related_ids=[s.id, result.submission_id],
                ))

        # 2. 抄袭检测（4-gram overlap）
        plag_pairs = self._detect_plagiarism(submissions)
        for s1, s2, overlap in plag_pairs:
            alerts.append(TeacherAlert(
                alert_type="plagiarism",
                severity="critical",
                message=f"学生 {s1.student_name} 与 {s2.student_name} 疑似抄袭 (相似度 {overlap:.1%})",
                related_ids=[s1.id, s2.id],
            ))
            # 标记抄袭
            s1.plagiarized = True
            s2.plagiarized = True
            self.db.save_submission(s1)
            self.db.save_submission(s2)

        # 3. 低提交率预警
        submit_rate = len([s for s in submissions
                          if s.status != SubmitStatus.PENDING]) / len(submissions)
        if submit_rate < 0.5:
            alerts.append(TeacherAlert(
                alert_type="low_submit_rate",
                severity="info",
                message=f"作业 {assignment_id} 提交率仅 {submit_rate:.0%}",
                related_ids=[assignment_id],
            ))

        return alerts

    def _detect_plagiarism(self, submissions: List,
                           threshold: float = 0.6,
                           min_length: int = 100) -> List:
        """
        改进版抄袭检测
        - 跳过同一学生的多次提交（自身去重）
        - 跳过过短内容（< min_length 字符）
        - 过滤公共短语（高频 n-gram 在超过30%提交中出现则排除）
        - 使用 overlap coefficient 替代 Jaccard（更敏感于包含关系）

        Returns: [(sub1, sub2, overlap_ratio)]
        """
        # 过滤有效提交：内容足够长
        valid = [s for s in submissions
                 if s.content and len(s.content) >= min_length]
        if len(valid) < 2:
            return []

        # 计算所有 n-gram 的文档频率，用于过滤公共短语
        all_ngram_sets = {}
        for s in valid:
            all_ngram_sets[s.id] = self._get_ngrams(s.content, 4)

        # 统计每个 n-gram 在多少个文档中出现
        from collections import Counter
        doc_freq = Counter()
        for ngrams in all_ngram_sets.values():
            doc_freq.update(ngrams)

        total_docs = len(valid)
        # 公共短语阈值：超过 30% 的文档都包含的 n-gram 视为公共短语
        common_threshold = total_docs * 0.3
        common_ngrams = {ng for ng, cnt in doc_freq.items()
                         if cnt > max(common_threshold, 2)}

        pairs = []
        seen_pairs = set()  # 避免重复检测

        for i in range(len(valid)):
            for j in range(i + 1, len(valid)):
                s1, s2 = valid[i], valid[j]

                # 自身去重：同一学生的不同提交不做抄袭比较
                if s1.student_id == s2.student_id:
                    continue

                # 避免重复
                pair_key = (min(s1.id, s2.id), max(s1.id, s2.id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                ngrams1 = all_ngram_sets[s1.id] - common_ngrams
                ngrams2 = all_ngram_sets[s2.id] - common_ngrams

                if not ngrams1 or not ngrams2:
                    continue

                # 使用 overlap coefficient: |A∩B| / min(|A|, |B|)
                # 比 Jaccard 更敏感于"小文本是大文本的子集"情况
                intersection = ngrams1 & ngrams2
                min_size = min(len(ngrams1), len(ngrams2))
                overlap = len(intersection) / min_size if min_size else 0

                if overlap > threshold:
                    pairs.append((s1, s2, overlap))

        return pairs

    def _get_ngrams(self, text: str, n: int = 4) -> set:
        """提取字符级 n-gram"""
        chars = list(text.replace(" ", "").replace("\n", ""))
        return set("".join(chars[i:i+n])
                   for i in range(len(chars) - n + 1))

    # ── 格式化输出 ──────────────────────────

    def format_progress_report(self, assignment_id: str) -> str:
        """格式化进度报告"""
        progress = self.get_assignment_progress(assignment_id)
        alerts = self.detect_anomalies(assignment_id)

        lines = [
            f"📊 作业进度报告 - {assignment_id}",
            "━━━━━━━━━━━━━━",
            f"📝 提交数: {progress['total_submissions']}",
            f"✅ 已批改: {progress['graded_count']}",
            f"⏳ 待批改: {progress['pending_count']}",
            f"🔔 待申诉: {progress['appeal_count']}",
            f"📈 平均分: {progress['avg_score']}",
            f"📊 分数范围: {progress['min_score']} ~ {progress['max_score']}",
        ]

        if alerts:
            lines.append("\n⚠️ 预警:")
            for a in alerts:
                icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(
                    a.severity, "⚠️"
                )
                lines.append(f"  {icon} [{a.alert_type}] {a.message}")

        return "\n".join(lines)
