"""
TA 渠道管理
处理申诉推送和 TA 审批指令
"""
from __future__ import annotations
import re
from typing import Optional, Tuple

from contracts.models import Appeal, HomeworkSubmission
from storage.db import DB


class TAChannelManager:
    """
    TA 渠道管理
    - 格式化申诉信息推送给 TA
    - 解析 TA 的审批指令（同意/驳回）
    """

    def __init__(self, db: DB):
        self.db = db

    def format_appeal_push(self, appeal: Appeal,
                           submission: Optional[HomeworkSubmission] = None) -> str:
        """
        格式化申诉信息，推送给 TA
        """
        lines = [
            "🔔 新的申诉请求",
            "━━━━━━━━━━━━━━",
            f"📋 申诉ID: {appeal.id}",
            f"👤 学生ID: {appeal.student_id}",
            f"📝 提交ID: {appeal.submission_id}",
            f"💬 申诉原因: {appeal.reason}",
        ]

        if submission:
            lines.extend([
                f"📊 当前分数: {submission.score or '未评分'}",
                f"💬 当前反馈: {(submission.feedback or '无')[:100]}",
            ])

        lines.extend([
            "",
            "请回复:",
            "  「同意」或「1」— 批准申诉并重新评分",
            "  「驳回」或「2」— 维持原评分",
        ])

        return "\n".join(lines)

    def parse_ta_instruction(self, ta_message: str) -> Tuple[str, Optional[str]]:
        """
        解析 TA 指令

        Returns:
            (action, note) — action 为 "approve" / "reject" / "unknown"
            note 为 TA 附带的备注（如有）
        """
        text = ta_message.strip()

        # 批准
        if re.search(r"同意|批准|通过|确认", text) or text == "1":
            # 尝试提取备注
            note = re.sub(r"^(同意|批准|通过|确认|1)[\s：:，,]*", "", text).strip()
            return "approve", note if note else None

        # 驳回
        if re.search(r"驳回|拒绝|不同意|否决", text) or text == "2":
            note = re.sub(r"^(驳回|拒绝|不同意|否决|2)[\s：:，,]*", "", text).strip()
            return "reject", note if note else None

        return "unknown", None

    def confirm_decision(self, appeal_id: str,
                         action: str, note: Optional[str] = None) -> str:
        """
        生成确认信息
        """
        if action == "approve":
            msg = f"✅ 申诉 {appeal_id} 已批准，将重新评分"
        elif action == "reject":
            msg = f"❌ 申诉 {appeal_id} 已驳回，维持原评分"
        else:
            msg = f"⚠️ 无法识别的指令，请回复「同意」或「驳回」"

        if note:
            msg += f"\n📝 TA备注: {note}"

        return msg

    def get_pending_appeals_summary(self) -> str:
        """获取待处理申诉摘要"""
        appeals = self.db.list_pending_appeals()
        if not appeals:
            return "📭 当前没有待处理的申诉"

        lines = [f"📬 待处理申诉: {len(appeals)} 件\n"]
        for a in appeals:
            lines.append(f"  • {a.id} | 学生 {a.student_id} | {a.reason[:30]}")

        return "\n".join(lines)
