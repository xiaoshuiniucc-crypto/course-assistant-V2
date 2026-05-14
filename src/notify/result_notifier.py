"""
Student-facing grading result notifications.
"""
from __future__ import annotations

from typing import List, Optional

from contracts.models import GradingResult, HomeworkSubmission, Rubric
from grading.evidence import build_grading_evidence
from runtime.localization import (
    localize_dimension_name,
    localize_user_text,
    translate_confidence_label,
)


class ResultNotifier:
    def __init__(self, adapter):
        self.adapter = adapter

    async def notify_student(
        self,
        submission: HomeworkSubmission,
        result: GradingResult,
        rubric: Optional[Rubric] = None,
    ) -> None:
        message = self._format_result(submission, result, rubric)
        await self.adapter.send_message(submission.student_id, message)

    def _format_result(
        self,
        submission: HomeworkSubmission,
        result: GradingResult,
        rubric: Optional[Rubric] = None,
    ) -> str:
        lines: List[str] = [
            "作业批改结果",
            f"学生: {submission.student_name}",
            f"提交ID: {submission.id}",
            f"总分: {result.total_score:.1f}",
        ]

        if result.dimension_scores:
            lines.append("分项得分:")
            for dim_name, score in result.dimension_scores.items():
                lines.append(f"- {localize_dimension_name(dim_name)}: {score:.1f}")

        lines.append(
            f"AI置信度: {translate_confidence_label(result.confidence_label)} "
            f"({result.confidence:.2f})"
        )

        if result.feedback:
            lines.append("评语:")
            lines.append(localize_user_text(result.feedback))

        evidence_lines = build_grading_evidence(result, rubric)
        if evidence_lines:
            lines.append("评分依据:")
            for item in evidence_lines[:6]:
                lines.append(f"- {localize_user_text(item)}")

        lines.append("如需申诉，请在规定时间内提交申诉说明。")
        return "\n".join(lines)

    def format_batch_summary(
        self,
        assignment_id: str,
        submissions: List[HomeworkSubmission],
    ) -> str:
        graded = [item for item in submissions if item.score is not None]
        pending = [item for item in submissions if item.score is None]
        avg_score = (
            sum(item.score for item in graded if item.score is not None) / len(graded)
            if graded else 0.0
        )

        lines = [
            "批改完成汇总",
            f"作业ID: {assignment_id}",
            f"已评分: {len(graded)}",
            f"未评分: {len(pending)}",
            f"平均分: {avg_score:.1f}",
        ]
        return "\n".join(lines)
