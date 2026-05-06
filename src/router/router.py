"""
Intent router with lightweight session state.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from contracts.models import IntentResult, IntentType, MessageType, QQMessage
from storage.db import DB


INTENT_RULES: List[Dict[str, Any]] = [
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
        "keywords": ["提交作业", "上传作业", "交作业", "作业提交", "交了", "交完了"],
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
        "intent": IntentType.GENERATE_STUDENT_REPORT,
        "keywords": ["我的报告", "个人报告", "学生报告", "雷达图", "课件依据", "成绩详情", "查看我的成绩"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.TA_APPROVE,
        "keywords": ["同意申诉", "批准申诉", "通过申诉", "同意：", "批准：", "1"],
        "tags": ["ta_channel"],
        "message_types": [MessageType.PRIVATE],
    },
    {
        "intent": IntentType.TA_REJECT,
        "keywords": ["驳回申诉", "拒绝申诉", "驳回：", "2"],
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
        "keywords": ["请教", "问一下", "请问", "解释一下", "帮我理解", "讲一下"],
        "tags": [],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
    {
        "intent": IntentType.TA_COMMAND,
        "keywords": ["/待审列表", "/pending", "/处理", "/handle", "/历史", "/history", "/统计", "/stats", "/帮助", "/help"],
        "tags": ["ta_channel"],
        "message_types": [MessageType.PRIVATE, MessageType.GROUP],
    },
]


class SessionState:
    WAITING_COURSE_CONFIRM = "waiting_course_confirm"
    WAITING_APPEAL_REASON = "waiting_appeal_reason"
    WAITING_RUBRIC_TEXT = "waiting_rubric_text"
    WAITING_RUBRIC_APPROVAL = "waiting_rubric_approval"
    WAITING_TA_REVIEW = "waiting_ta_review"
    NONE = None


class IntentRouter:
    def __init__(self, db: DB):
        self.db = db

    def classify(self, message: QQMessage) -> IntentResult:
        session_id = self._get_session_id(message)
        session = self.db.get_session(session_id)
        if session and session.get("waiting_for"):
            return self._handle_multi_turn(message, session, session_id)

        best_intent = IntentType.UNKNOWN
        best_score = 0.0
        best_slots: Dict[str, Any] = {}
        content = message.content.strip()

        file_intent = self._classify_file_message(message, session_id)
        if file_intent is not None:
            return file_intent

        for rule in INTENT_RULES:
            score = 0.0
            for kw in rule["keywords"]:
                if kw in content:
                    score += 0.4

            if rule["intent"] == IntentType.ASK_QUESTION and score > 0:
                score *= 0.6

            if message.message_type in rule.get("message_types", []):
                score += 0.1

            if rule.get("tags"):
                if "file" in rule["tags"] and message.file_url:
                    score += 0.3
                if "ta_channel" in rule["tags"] and self._is_ta_message(message):
                    score += 0.5

            if score > best_score:
                best_score = score
                best_intent = rule["intent"]
                best_slots = self._extract_slots(best_intent, content)

        confidence = min(best_score, 1.0) if best_score > 0 else 0.0
        if confidence < 0.2:
            best_intent = IntentType.UNKNOWN
            confidence = 0.0
        elif best_intent == IntentType.ASK_QUESTION and not self._looks_like_question(content):
            best_intent = IntentType.UNKNOWN
            confidence = 0.0

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

    def _classify_file_message(
        self,
        message: QQMessage,
        session_id: str,
    ) -> IntentResult | None:
        if not message.file_url:
            return None

        slots: Dict[str, Any] = {
            "file_url": message.file_url,
        }
        if message.file_name:
            slots["file_name"] = message.file_name

        content = (message.content or "").strip()
        is_group = message.message_type == MessageType.GROUP
        is_private = message.message_type == MessageType.PRIVATE

        if is_group:
            intent = IntentType.UPLOAD_COURSEWARE
            confidence = 0.9
        elif is_private:
            if self._looks_like_courseware_upload(content) or self._looks_like_courseware_file(message.file_name):
                intent = IntentType.UPLOAD_COURSEWARE
                confidence = 0.95
            else:
                intent = IntentType.SUBMIT_HOMEWORK
                confidence = 0.85
        else:
            return None

        self.db.save_session(
            session_id=session_id,
            user_id=message.user_id,
            intent=intent,
            slots=slots,
        )
        return IntentResult(
            intent=intent,
            confidence=confidence,
            session_id=session_id,
            slots=slots,
        )

    def _handle_multi_turn(self, message: QQMessage, session: Dict, session_id: str) -> IntentResult:
        waiting = session.get("waiting_for")
        slots = session.get("slots", {})

        if waiting == SessionState.WAITING_COURSE_CONFIRM:
            if any(kw in message.content for kw in ["确认", "是", "对", "好的"]):
                slots["confirmed"] = True
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=IntentType.UPLOAD_COURSEWARE,
                    waiting_for=None,
                    slots=slots,
                )
                return IntentResult(IntentType.UPLOAD_COURSEWARE, 0.9, session_id, slots=slots)
            if any(kw in message.content for kw in ["取消", "不", "算了"]):
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=None,
                    waiting_for=None,
                    slots={},
                )
                return IntentResult(IntentType.UNKNOWN, 0.9, session_id)

        elif waiting == SessionState.WAITING_APPEAL_REASON:
            slots["appeal_reason"] = message.content
            self.db.save_session(
                session_id=session_id,
                user_id=message.user_id,
                intent=IntentType.APPEAL,
                waiting_for=None,
                slots=slots,
            )
            return IntentResult(IntentType.APPEAL, 0.9, session_id, slots=slots)

        elif waiting == SessionState.WAITING_RUBRIC_TEXT:
            slots["rubric_text"] = message.content
            self.db.save_session(
                session_id=session_id,
                user_id=message.user_id,
                intent=IntentType.SET_RUBRIC,
                waiting_for=None,
                slots=slots,
            )
            return IntentResult(IntentType.SET_RUBRIC, 0.9, session_id, slots=slots)

        elif waiting == SessionState.WAITING_RUBRIC_APPROVAL:
            slots["teacher_reply"] = message.content
            self.db.save_session(
                session_id=session_id,
                user_id=message.user_id,
                intent=IntentType.SET_RUBRIC,
                waiting_for=SessionState.WAITING_RUBRIC_APPROVAL,
                slots=slots,
            )
            return IntentResult(
                IntentType.SET_RUBRIC,
                0.95,
                session_id,
                waiting_for=SessionState.WAITING_RUBRIC_APPROVAL,
                slots=slots,
            )

        elif waiting == SessionState.WAITING_TA_REVIEW:
            content = message.content.strip()
            if any(kw in content for kw in ["同意申诉", "批准申诉", "通过申诉", "同意：", "批准：", "1"]):
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=IntentType.TA_APPROVE,
                    waiting_for=None,
                    slots=slots,
                )
                return IntentResult(IntentType.TA_APPROVE, 0.95, session_id, slots=slots)
            if any(kw in content for kw in ["驳回申诉", "拒绝申诉", "驳回：", "2"]):
                self.db.save_session(
                    session_id=session_id,
                    user_id=message.user_id,
                    intent=IntentType.TA_REJECT,
                    waiting_for=None,
                    slots=slots,
                )
                return IntentResult(IntentType.TA_REJECT, 0.95, session_id, slots=slots)

        self.db.save_session(session_id=session_id, user_id=message.user_id, waiting_for=None)
        return IntentResult(IntentType.UNKNOWN, 0.3, session_id)

    def _get_session_id(self, message: QQMessage) -> str:
        if message.group_id:
            return f"sess_{message.user_id}_{message.group_id}"
        return f"sess_{message.user_id}"

    def _is_ta_message(self, message: QQMessage) -> bool:
        session_id = self._get_session_id(message)
        session = self.db.get_session(session_id)
        if session and session.get("waiting_for") == SessionState.WAITING_TA_REVIEW:
            return True

        content = message.content.strip()
        ta_approve_patterns = ["同意申诉", "批准申诉", "通过申诉", "同意：", "批准："]
        ta_reject_patterns = ["驳回申诉", "拒绝申诉", "驳回："]
        return any(pattern in content for pattern in ta_approve_patterns + ta_reject_patterns)

    def _looks_like_question(self, content: str) -> bool:
        stripped = content.strip()
        if not stripped:
            return False
        if stripped.endswith(("?", "？")):
            return True
        question_patterns = [
            "什么是", "请教", "请问", "问一下", "解释一下", "帮我理解",
            "能否", "是否", "为什么", "怎么", "如何", "哪里", "哪种",
        ]
        return any(pattern in stripped for pattern in question_patterns)

    def _looks_like_courseware_upload(self, content: str) -> bool:
        if not content:
            return False

        lowered = content.lower()
        courseware_markers = (
            "课件",
            "课程资料",
            "讲义",
            "上传课件",
            "上传资料",
            "courseware",
            "material",
            "rubric",
            "评分细则",
        )
        return any(marker in lowered for marker in courseware_markers)

    def _looks_like_courseware_file(self, file_name: str | None) -> bool:
        if not file_name:
            return False

        lowered = file_name.lower()
        courseware_file_markers = (
            "课件",
            "讲义",
            "课程",
            "资料",
            "chapter",
            "lecture",
            "slides",
            "courseware",
            "syllabus",
        )
        return any(marker in lowered for marker in courseware_file_markers)

    def _extract_slots(self, intent: str, content: str) -> Dict[str, Any]:
        slots: Dict[str, Any] = {}
        if intent == IntentType.SUBMIT_HOMEWORK:
            match = re.search(r"作业[_\s]*(\w+)", content)
            if match:
                slots["assignment_id"] = match.group(1)
        elif intent == IntentType.APPEAL:
            match = re.search(r"申诉[：:]\s*(.+)", content)
            if match:
                slots["appeal_reason"] = match.group(1).strip()
            else:
                slots["need_reason"] = True
        elif intent == IntentType.VIEW_REPORT:
            match = re.search(r"报告[：:]\s*(\w+)", content)
            if match:
                slots["report_id"] = match.group(1)
        elif intent == IntentType.UPLOAD_COURSEWARE:
            local_path_match = re.search(r"([A-Za-z]:\\[^\s]+|\.?[\\/][^\s]+)", content)
            if local_path_match:
                slots["file_path"] = local_path_match.group(1)
        return slots

    def set_waiting(
        self,
        session_id: str,
        user_id: str,
        waiting_for: str,
        intent: str = None,
        slots: Dict = None,
    ):
        self.db.save_session(
            session_id=session_id,
            user_id=user_id,
            intent=intent,
            waiting_for=waiting_for,
            slots=slots or {},
        )
