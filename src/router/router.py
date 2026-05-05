"""
意图路由器
基于规则的消息意图识别 + 多轮对话状态机
"""
from __future__ import annotations
import re
import uuid
from typing import Optional, Dict, Any, List

from contracts.models import IntentResult, IntentType, MessageType, QQMessage
from storage.db import DB


# ─────────────────────────────────────────────
# 意图规则定义
# ─────────────────────────────────────────────

INTENT_RULES: List[Dict] = [
    {
        "intent": IntentType.UPLOAD_COURSEWARE,
        "keywords": ["上传课件", "添加课件", "上传资料", "发课件", "新增课件"],
        "tags": ["file"],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.SET_RUBRIC,
        "keywords": ["设置评分", "评分标准", "设置标准", "评分规则", "定义标准"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.SET_ASSIGNMENT,
        "keywords": ["布置作业", "发布作业", "设置作业", "新建作业"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.SUBMIT_HOMEWORK,
        "keywords": ["提交作业", "交作业", "作业提交", "交了", "交完了"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.APPEAL,
        "keywords": ["申诉", "复议", "不同意评分", "分数不对", "申请复查"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.VIEW_REPORT,
        "keywords": ["查看报告", "成绩报告", "批改报告", "统计报告", "生成报告"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.TA_APPROVE,
        "keywords": ["同意申诉", "批准申诉", "通过申诉", "同意，", "批准，", "1"],
        "tags": ["ta_channel"],
        "message_types": [MessageType.PRIVATE],
    },
    {
        "intent": IntentType.TA_REJECT,
        "keywords": ["驳回申诉", "拒绝申诉", "驳回，", "2"],
        "tags": ["ta_channel"],
        "message_types": [MessageType.PRIVATE],
    },
    {
        "intent": IntentType.QUERY_PROGRESS,
        "keywords": ["进度", "查询进度", "学习进度", "完成情况", "提交情况"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.ASK_QUESTION,
        "keywords": ["问题", "提问", "请教", "问一下", "怎么", "如何", "什么是", "为什么"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
]


# ─────────────────────────────────────────────
# 多轮对话状态
# ─────────────────────────────────────────────

class SessionState:
    """多轮对话等待状态"""
    WAITING_COURSE_CONFIRM = "waiting_course_confirm"
    WAITING_APPEAL_REASON = "waiting_appeal_reason"
    WAITING_RUBRIC_TEXT = "waiting_rubric_text"
    WAITING_ASSIGNMENT_DETAIL = "waiting_assignment_detail"
    WAITING_TA_REVIEW = "waiting_ta_review"
    NONE = None


class IntentRouter:
    """
    意图路由器
    - 规则匹配（关键词 + 消息类型 + 标签）
    - 多轮会话管理
    """

    def __init__(self, db: DB):
        self.db = db

    def classify(self, message: QQMessage) -> IntentResult:
        """
        对消息进行意图分类
        1. 检查多轮对话状态
        2. 规则匹配
        3. 返回意图结果
        """
        session_id = self._get_session_id(message)
        session = self.db.get_session(session_id)

        # 多轮对话优先
        if session and session.get("waiting_for"):
            return self._handle_multi_turn(message, session, session_id)

        # 规则匹配
        best_intent = IntentType.UNKNOWN
        best_score = 0.0
        best_slots: Dict[str, Any] = {}

        content = message.content.strip()
        content_lower = content.lower()

        for rule in INTENT_RULES:
            score = 0.0

            # 关键词匹配
            for kw in rule["keywords"]:
                if kw in content:
                    score += 0.4

            # 消息类型匹配
            if message.message_type in rule.get("message_types", []):
                score += 0.1

            # 标签匹配（如 file 附件、ta_channel）
            if rule.get("tags"):
                if "file" in rule["tags"] and message.file_url:
                    score += 0.3
                if "ta_channel" in rule["tags"] and self._is_ta_message(message):
                    score += 0.5

            if score > best_score:
                best_score = score
                best_intent = rule["intent"]
                best_slots = self._extract_slots(best_intent, content)

        # 置信度阈值
        confidence = min(best_score, 1.0) if best_score > 0 else 0.0
        if confidence < 0.2:
            best_intent = IntentType.UNKNOWN
            confidence = 0.0

        # 保存会话
        self.db.save_session(
            session_id=session_id,
            user_id=message.user_id,
            intent=best_intent,
            slots=best_slots,
        )

        return IntentResult(
            intent=best_intent,
            confidence=confidence,
            session_id=session_id,
            slots=best_slots,
        )

    # ── 多轮对话 ────────────────────────────

    def _handle_multi_turn(self, message: QQMessage,
                           session: Dict,
                           session_id: str) -> IntentResult:
        """处理多轮对话"""
        waiting = session.get("waiting_for")
        slots = session.get("slots", {})

        if waiting == SessionState.WAITING_COURSE_CONFIRM:
            # 确认上传课件
            if any(kw in message.content for kw in ["确认", "是", "对", "好的"]):
                slots["confirmed"] = True
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=IntentType.UPLOAD_COURSEWARE,
                    waiting_for=None,
                    slots=slots,
                )
                return IntentResult(
                    intent=IntentType.UPLOAD_COURSEWARE,
                    confidence=0.9,
                    session_id=session_id,
                    slots=slots,
                )
            elif any(kw in message.content for kw in ["取消", "不", "算了"]):
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=None,
                    waiting_for=None,
                    slots={},
                )
                return IntentResult(
                    intent=IntentType.UNKNOWN,
                    confidence=0.9,
                    session_id=session_id,
                )

        elif waiting == SessionState.WAITING_APPEAL_REASON:
            # 收到申诉原因
            slots["appeal_reason"] = message.content
            self.db.save_session(
                session_id=session_id,
                user_id=message.user_id,
                intent=IntentType.APPEAL,
                waiting_for=None,
                slots=slots,
            )
            return IntentResult(
                intent=IntentType.APPEAL,
                confidence=0.9,
                session_id=session_id,
                slots=slots,
            )

        elif waiting == SessionState.WAITING_RUBRIC_TEXT:
            # 收到评分标准文本
            slots["rubric_text"] = message.content
            self.db.save_session(
                session_id=session_id,
                user_id=message.user_id,
                intent=IntentType.SET_RUBRIC,
                waiting_for=None,
                slots=slots,
            )
            return IntentResult(
                intent=IntentType.SET_RUBRIC,
                confidence=0.9,
                session_id=session_id,
                slots=slots,
            )

        elif waiting == SessionState.WAITING_TA_REVIEW:
            # TA 审批回复
            content = message.content.strip()
            if any(kw in content for kw in ["同意申诉", "批准申诉", "通过申诉", "同意，", "批准，", "1"]):
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=IntentType.TA_APPROVE,
                    waiting_for=None,
                    slots=slots,
                )
                return IntentResult(
                    intent=IntentType.TA_APPROVE,
                    confidence=0.95,
                    session_id=session_id,
                    slots=slots,
                )
            elif any(kw in content for kw in ["驳回申诉", "拒绝申诉", "驳回，", "2"]):
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=IntentType.TA_REJECT,
                    waiting_for=None,
                    slots=slots,
                )
                return IntentResult(
                    intent=IntentType.TA_REJECT,
                    confidence=0.95,
                    session_id=session_id,
                    slots=slots,
                )

        # 兜底: 清除等待状态
        self.db.save_session(
            session_id=session_id,
            user_id=message.user_id,
            waiting_for=None,
        )
        return IntentResult(
            intent=IntentType.UNKNOWN,
            confidence=0.3,
            session_id=session_id,
        )

    # ── 辅助方法 ────────────────────────────

    def _get_session_id(self, message: QQMessage) -> str:
        """生成会话ID（用户+群）"""
        if message.group_id:
            return f"sess_{message.user_id}_{message.group_id}"
        return f"sess_{message.user_id}"

    def _is_ta_message(self, message: QQMessage) -> bool:
        """判断是否是 TA 渠道消息
        通过会话上下文判断: 如果该用户当前会话处于等待 TA 审批状态
        """
        session_id = self._get_session_id(message)
        session = self.db.get_session(session_id)
        if session and session.get("waiting_for") == "waiting_ta_review":
            return True
        # 也检查消息内容中是否包含 TA 审批关键词组合
        content = message.content.strip()
        ta_approve_patterns = ["同意申诉", "批准申诉", "通过申诉", "同意，", "批准，"]
        ta_reject_patterns = ["驳回申诉", "拒绝申诉", "驳回，"]
        for p in ta_approve_patterns + ta_reject_patterns:
            if p in content:
                return True
        return False

    def _extract_slots(self, intent: str,
                       content: str) -> Dict[str, Any]:
        """从消息中提取槽位"""
        slots: Dict[str, Any] = {}
        if intent == IntentType.SUBMIT_HOMEWORK:
            # 尝试提取作业ID
            m = re.search(r"作业[_\s]*(\w+)", content)
            if m:
                slots["assignment_id"] = m.group(1)
        elif intent == IntentType.APPEAL:
            m = re.search(r"申诉[：:]\s*(.+)", content)
            if m:
                slots["appeal_reason"] = m.group(1).strip()
            else:
                slots["need_reason"] = True
        elif intent == IntentType.VIEW_REPORT:
            m = re.search(r"报告[：:]\s*(\w+)", content)
            if m:
                slots["report_id"] = m.group(1)
        return slots

    def set_waiting(self, session_id: str, user_id: str,
                    waiting_for: str, intent: str = None,
                    slots: Dict = None):
        """设置多轮对话等待状态"""
        self.db.save_session(
            session_id=session_id,
            user_id=user_id,
            intent=intent,
            waiting_for=waiting_for,
            slots=slots or {},
        )
