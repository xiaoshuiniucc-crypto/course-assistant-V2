"""
结果通知器
通过 QQ Bot 将批改结果推送给学生
"""
from __future__ import annotations
from typing import Optional

from contracts.models import GradingResult, HomeworkSubmission, Rubric


class ResultNotifier:
    """批改结果通知"""

    def __init__(self, qclaw_adapter=None):
        """
        Args:
            qclaw_adapter: QclawAdapter 实例，用于发送 QQ 消息
        """
        self.adapter = qclaw_adapter

    async def notify_student(
        self,
        submission: HomeworkSubmission,
        result: GradingResult,
        rubric: Optional[Rubric] = None,
    ) -> str:
        """通知学生批改结果，返回通知文本"""
        msg = self._format_result(submission, result, rubric)

        if self.adapter:
            try:
                await self.adapter.send_message(
                    user_id=submission.student_id,
                    content=msg,
                )
            except Exception as e:
                msg += f"\n[通知发送失败: {e}]"

        return msg

    def _format_result(self, submission: HomeworkSubmission,
                       result: GradingResult,
                       rubric: Optional[Rubric] = None) -> str:
        """格式化批改结果"""
        lines = [
            f"📊 作业批改结果",
            f"━━━━━━━━━━━━━━",
            f"📝 作业ID: {submission.assignment_id}",
            f"👤 学生: {submission.student_name}",
            f"📈 总分: {result.total_score}/100",
            "",
        ]

        # 维度评分详情
        if result.dimension_scores:
            lines.append("📋 维度评分:")
            for dim_name, score in result.dimension_scores.items():
                bar_len = int(score / 100 * 20)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                lines.append(f"  {dim_name}: {score:.1f} |{bar}|")

        # 反馈
        if result.feedback:
            lines.append(f"\n💬 评语: {result.feedback}")

        # 申诉提示
        lines.append("\n如对评分有异议，请回复「申诉 + 原因」")

        return "\n".join(lines)

    def format_batch_summary(self, results: list,
                             assignment_title: str = "") -> str:
        """格式化批量批改摘要"""
        if not results:
            return "暂无批改结果"

        scores = [r.total_score for r in results]
        avg = sum(scores) / len(scores)
        lines = [
            f"📊 批改完成摘要 - {assignment_title}",
            f"━━━━━━━━━━━━━━",
            f"✅ 批改份数: {len(results)}",
            f"📈 平均分: {avg:.1f}",
            f"📊 最高分: {max(scores):.1f}",
            f"📉 最低分: {min(scores):.1f}",
        ]
        return "\n".join(lines)
