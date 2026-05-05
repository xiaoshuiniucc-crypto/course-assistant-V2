"""
M02 — 意图识别路由
解析用户消息，识别操作意图，路由到对应处理模块。管理多轮会话状态。

MVP 策略：
- 基于规则匹配（关键词 + 标签 + 消息类型），不依赖 LLM
- 会话状态用内存字典存储（生产环境换 Redis）
"""

import re
from datetime import datetime
from typing import Optional

from contracts.models import Intent, Session
from qclaw.adapter import QQMessage


# ── 意图规则定义 ────────────────────────────────────────────

INTENT_RULES = [
    # (意图名, 匹配条件, 参数提取规则)
    ("UPLOAD_COURSEWARE", {"tag": "#课件", "is_file": True}, {}),
    ("SET_RUBRIC",        {"tag": "#评分细则", "is_file": True}, {}),
    ("SET_ASSIGNMENT",    {"tag": "#作业要求"}, {}),
    ("SUBMIT_HOMEWORK",   {"msg_type": "private", "is_file": True}, {}),
    ("APPEAL",            {"text_match": r"^(申诉|appeal)"}, {}),
    ("VIEW_REPORT",       {"text_match": r"^(报告|查看报告|report)"}, {}),
    ("TA_APPROVE",        {"msg_type": "channel", "text_match": r"^(同意|1|批准)"}, {}),
    ("TA_REJECT",         {"msg_type": "channel", "text_match": r"^(驳回|2)"}, {}),
    ("QUERY_PROGRESS",    {"text_match": r"(进度|统计|查询)"}, {}),
    ("ASK_QUESTION",      {"text_match": r"^(提问|问一下|help)"}, {}),
]


class IntentRouter:
    """
    意图识别路由器。
    - classify(): 从消息中识别意图
    - 会话管理：get/set/clear session
    """

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    # ── 意图分类 ──────────────────────────────────────────

    def classify(self, message: QQMessage, sender_role: str = "student") -> Intent:
        """
        从消息中识别意图。
        优先级：标签匹配 > 文件消息规则 > 文本匹配 > 会话状态 > 默认
        """
        # 1. 检查标签（最高优先级）
        if message.tag:
            for rule_name, conditions, _ in INTENT_RULES:
                if conditions.get("tag") == message.tag:
                    return Intent(action=rule_name, params=self._extract_params(message, rule_name))

        # 2. 文件消息（私聊文件 = 提交作业）
        if message.is_file and message.msg_type == "private":
            return Intent(action="SUBMIT_HOMEWORK",
                         params={"file_id": message.file_id, "file_name": message.file_name})

        # 3. 频道消息（助教操作）
        if message.msg_type == "channel":
            return self._classify_channel_message(message)

        # 4. 文本匹配规则
        for rule_name, conditions, _ in INTENT_RULES:
            if "text_match" in conditions:
                pattern = conditions["text_match"]
                if re.search(pattern, message.text, re.IGNORECASE):
                    return Intent(action=rule_name,
                                 params=self._extract_params(message, rule_name))

        # 5. 检查会话状态（多轮对话）
        session = self.get_session(message.sender_qq)
        if session and session.state != "IDLE":
            return self._classify_from_session(message, session)

        # 6. 默认：未知意图
        return Intent(action="UNKNOWN", params={"text": message.text}, confidence=0.3)

    # ── 会话管理 ──────────────────────────────────────────

    def get_session(self, user_qq: str) -> Optional[Session]:
        """获取用户会话。"""
        return self._sessions.get(user_qq)

    def set_session(self, user_qq: str, state: str, data: Optional[dict] = None) -> None:
        """设置/更新用户会话状态。"""
        existing = self._sessions.get(user_qq)
        if existing:
            existing.state = state
            if data:
                existing.data.update(data)
            existing.updated_at = datetime.now()
        else:
            self._sessions[user_qq] = Session(
                user_qq=user_qq, state=state,
                data=data or {}, updated_at=datetime.now(),
            )

    def clear_session(self, user_qq: str) -> None:
        """清除用户会话。"""
        self._sessions.pop(user_qq, None)

    # ── 内部方法 ──────────────────────────────────────────

    def _classify_channel_message(self, message: QQMessage) -> Intent:
        """频道消息分类：助教审批操作。"""
        text = message.text.strip()
        if re.match(r"^(同意|1|批准)", text):
            note = ""
            return Intent(action="TA_APPROVE", params={"ta_qq": message.sender_qq, "note": note})
        elif re.match(r"^(驳回|2)", text):
            # 提取驳回原因
            note = re.sub(r"^(驳回|2)\s*", "", text).strip()
            return Intent(action="TA_REJECT", params={"ta_qq": message.sender_qq, "note": note})
        else:
            return Intent(action="UNKNOWN", params={"text": text}, confidence=0.3)

    def _classify_from_session(self, message: QQMessage, session: Session) -> Intent:
        """根据会话状态推断意图。"""
        if session.state == "WAITING_COURSE_CONFIRM":
            # 学生选择课程号
            return Intent(action="CONFIRM_COURSE",
                         params={"course_choice": message.text.strip()})
        elif session.state == "WAITING_APPEAL_REASON":
            # 学生提交申诉理由
            return Intent(action="SUBMIT_APPEAL_REASON",
                         params={"appeal_reason": message.text.strip()})
        else:
            return Intent(action="UNKNOWN", params={"text": message.text}, confidence=0.5)

    def _extract_params(self, message: QQMessage, rule_name: str) -> dict:
        """从消息中提取参数。"""
        params = {"sender_qq": message.sender_qq}
        if message.file_id:
            params["file_id"] = message.file_id
        if message.file_name:
            params["file_name"] = message.file_name
        if message.group_id:
            params["group_id"] = message.group_id
        if message.text:
            params["text"] = message.text
        return params
