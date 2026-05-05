"""
M09 — 助教频道管理
在助教专属频道推送待审申诉，解析助教操作指令。

MVP 策略：
- 推送消息格式化为文本
- 助教指令解析基于规则匹配（同意/1/批准 → approve, 驳回/2 + 原因 → reject）
- 与 M01 QclawAdapter 配合发送频道消息
"""

import re
from typing import Optional

from contracts.models import AppealRecord, GradingResult, TAInstruction


class TAChannelManager:
    """
    助教频道管理器。
    - push_appeal(): 推送申诉到频道
    - parse_ta_instruction(): 解析助教指令
    - confirm_decision(): 确认决定并更新状态
    """

    def __init__(self, qclaw_adapter=None, channel_id: str = "ta_channel"):
        """
        Args:
            qclaw_adapter: M01 QclawAdapter 实例（用于发送频道消息）
            channel_id: 助教频道 ID
        """
        self._adapter = qclaw_adapter
        self._channel_id = channel_id

    # ── 推送申诉 ──────────────────────────────────────────

    def push_appeal(self, appeal: AppealRecord,
                    original_result: Optional[GradingResult] = None,
                    regrade_result: Optional[GradingResult] = None,
                    student_name: str = "学生") -> str:
        """
        格式化申诉信息并推送到助教频道。
        返回格式化后的消息文本。
        """
        # 计算分差
        score_diff = 0
        new_score_str = "待重评"
        if regrade_result and appeal.ai_new_score is not None:
            score_diff = appeal.ai_new_score - appeal.original_score
            new_score_str = str(appeal.ai_new_score)

        # 构建变更说明
        change_desc = ""
        if regrade_result:
            change_parts = []
            for dim in regrade_result.dimensions:
                change_parts.append(f"  · {dim.name}：{dim.score}/{dim.max_score} — {dim.dimension_comment}")
            change_desc = "\n".join(change_parts)

        msg = f"""【申诉待审】学生 {student_name}
原分：{appeal.original_score} → AI重评：{new_score_str}（{score_diff:+d}分）
申诉理由：{appeal.student_reason}
置信度：{regrade_result.confidence if regrade_result else '未知'}

变更说明：
{change_desc if change_desc else '（等待AI重评）'}

回复「同意」或「1」→ 批准
回复「驳回 + 原因」→ 驳回"""

        # 发送频道消息
        if self._adapter:
            self._adapter.send_channel(self._channel_id, msg)

        return msg

    # ── 解析助教指令 ──────────────────────────────────────

    def parse_ta_instruction(self, message: str) -> TAInstruction:
        """
        解析助教的频道消息为结构化指令。
        - "同意" / "1" / "批准" → approve
        - "驳回 原因" / "2 原因" → reject
        """
        text = message.strip()

        # 批准
        if re.match(r'^(同意|1|批准)', text):
            note = re.sub(r'^(同意|1|批准)\s*', '', text).strip()
            return TAInstruction(action="approve", note=note)

        # 驳回
        if re.match(r'^(驳回|2)', text):
            note = re.sub(r'^(驳回|2)\s*', '', text).strip()
            return TAInstruction(action="reject", note=note)

        # 无法识别
        return TAInstruction(action="unknown", note=text)

    # ── 确认决定 ──────────────────────────────────────────

    def confirm_decision(self, appeal_id: int, instruction: TAInstruction,
                         regrade_result: Optional[GradingResult] = None,
                         original_score: int = 0) -> dict:
        """
        确认助教决定，更新数据库并通知学生。
        返回操作结果摘要。
        """
        from storage import db

        if instruction.action == "approve":
            new_score = regrade_result.total_score if regrade_result else original_score
            db.resolve_appeal(appeal_id, "approved", instruction.note or "同意AI重评结果", new_score)
            result = {
                "status": "approved",
                "new_score": new_score,
                "note": instruction.note,
            }
        elif instruction.action == "reject":
            db.resolve_appeal(appeal_id, "rejected", instruction.note or "助教驳回", original_score)
            result = {
                "status": "rejected",
                "new_score": original_score,
                "note": instruction.note,
            }
        else:
            result = {
                "status": "unknown",
                "note": "无法识别的指令",
            }

        return result
