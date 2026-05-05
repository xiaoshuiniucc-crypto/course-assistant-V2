"""
QQ 课程助手 MVP 主入口
整合所有模块，处理消息循环
"""
from __future__ import annotations
import asyncio
import logging
import sys
import os
from typing import Optional

# 添加 src 到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qclaw.adapter import QclawAdapter
from router.router import IntentRouter
from knowledge.knowledge_base import KnowledgeBase
from ta_channel.ta_channel import TAChannelManager
from teacher_stats.stats import TeacherStats
from report.report_gen import ReportGenerator
from storage.db import DB
from rubric.rubric_parser import RubricParser
from parser.file_parser import FileParser
from homework.receiver import HomeworkReceiver
from grading.engine import GradingEngine
from notify.result_notifier import ResultNotifier
from appeal.handler import AppealHandler

from contracts.models import (
    QQMessage, IntentType, MessageType, SubmitStatus
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("main")


class QQCourseAssistant:
    """QQ 课程助手主类"""

    def __init__(self, db_path: str = "qq_course.db"):
        # 存储层
        self.db = DB(db_path)

        # 模块初始化
        self.qclaw = QclawAdapter()
        self.router = IntentRouter(self.db)
        self.knowledge = KnowledgeBase(self.db)
        self.ta_channel = TAChannelManager(self.db)
        self.teacher_stats = TeacherStats(self.db)
        self.report_gen = ReportGenerator(self.db)
        self.rubric_parser = RubricParser()
        self.file_parser = FileParser()
        self.homework = HomeworkReceiver(self.db)
        self.grading = GradingEngine(self.db)
        self.notifier = ResultNotifier(self.qclaw)
        self.appeal_handler = AppealHandler(self.db, self.grading)

        # 注册消息处理器
        self.qclaw.on_message(self._handle_message)

    async def _handle_message(self, msg: QQMessage):
        """核心消息处理循环"""
        try:
            # M02: 意图识别
            intent = self.router.classify(msg)
            logger.info(
                f"意图: {intent.intent} "
                f"(置信度 {intent.confidence:.2f})"
            )

            # M02 → 各模块分发
            if intent.intent == IntentType.UPLOAD_COURSEWARE:
                await self._handle_upload_courseware(msg, intent)
            elif intent.intent == IntentType.SET_RUBRIC:
                await self._handle_set_rubric(msg, intent)
            elif intent.intent == IntentType.SET_ASSIGNMENT:
                await self._handle_set_assignment(msg, intent)
            elif intent.intent == IntentType.SUBMIT_HOMEWORK:
                await self._handle_submit_homework(msg, intent)
            elif intent.intent == IntentType.APPEAL:
                await self._handle_appeal(msg, intent)
            elif intent.intent == IntentType.VIEW_REPORT:
                await self._handle_view_report(msg, intent)
            elif intent.intent in (IntentType.TA_APPROVE, IntentType.TA_REJECT):
                await self._handle_ta_action(msg, intent)
            elif intent.intent == IntentType.QUERY_PROGRESS:
                await self._handle_query_progress(msg, intent)
            elif intent.intent == IntentType.ASK_QUESTION:
                await self._handle_ask_question(msg, intent)
            else:
                await self.qclaw.send_message(
                    msg.user_id,
                    "抱歉，我没有理解您的意思。"
                    "可以试试:\n"
                    "• 上传课件\n• 设置评分标准\n"
                    "• 布置作业\n• 提交作业\n"
                    "• 查看报告\n• 查询进度"
                )

        except Exception as e:
            logger.error(f"消息处理异常: {e}", exc_info=True)
            await self.qclaw.send_message(
                msg.user_id, f"处理出错: {e}"
            )

    # ── 各意图处理器 ────────────────────────

    async def _handle_upload_courseware(self, msg: QQMessage, intent):
        """处理课件上传"""
        await self.qclaw.send_message(
            msg.user_id,
            "课件上传功能需要文件附件支持。\n"
            "请将课件文件发送给我（支持 PDF/DOCX/TXT/MD）"
        )

    async def _handle_set_rubric(self, msg: QQMessage, intent):
        """处理评分标准设置"""
        if intent.slots.get("rubric_text"):
            rubric = self.rubric_parser.parse(intent.slots["rubric_text"])
            self.db.save_rubric(rubric)
            await self.qclaw.send_message(
                msg.user_id,
                f"评分标准已设置:\n{self.rubric_parser.format_rubric(rubric)}"
            )
        else:
            from router.router import SessionState
            self.router.set_waiting(
                intent.session_id, msg.user_id,
                SessionState.WAITING_RUBRIC_TEXT,
                intent=IntentType.SET_RUBRIC
            )
            await self.qclaw.send_message(
                msg.user_id,
                "请输入评分标准，格式:\n"
                "维度名:权重:描述\n"
                "例如:\n"
                "内容准确性:0.4:答案是否正确\n"
                "逻辑性:0.3:推理是否合理\n"
                "表达:0.3:语言是否清晰"
            )

    async def _handle_set_assignment(self, msg: QQMessage, intent):
        """处理作业布置"""
        import uuid
        from contracts.models import Assignment
        from datetime import datetime

        assignment = Assignment(
            id=f"asgn_{uuid.uuid4().hex[:8]}",
            title=msg.content.replace("布置作业", "").strip() or "新作业",
            description=msg.content,
            rubric_id=None,
            courseware_id=None,
            deadline=None,
            created_at=datetime.now(),
        )
        self.db.save_assignment(assignment)
        await self.qclaw.send_message(
            msg.user_id,
            f"作业已创建: {assignment.title} (ID: {assignment.id})"
        )

    async def _handle_submit_homework(self, msg: QQMessage, intent):
        """处理作业提交"""
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(
                msg.user_id, "暂无可提交的作业"
            )
            return

        assignment = assignments[-1]  # 取最新的
        content = msg.content.replace("提交作业", "").strip()
        if not content:
            content = "（学生提交内容）"

        submission = self.homework.receive(
            assignment_id=assignment.id,
            student_id=msg.user_id,
            student_name=f"学生_{msg.user_id[:4]}",
            content=content,
        )
        await self.qclaw.send_message(
            msg.user_id,
            f"作业已提交! 提交ID: {submission.id}\n"
            f"状态: {submission.status}"
        )

    async def _handle_appeal(self, msg: QQMessage, intent):
        """处理申诉"""
        reason = intent.slots.get("appeal_reason", "")
        if not reason:
            from router.router import SessionState
            self.router.set_waiting(
                intent.session_id, msg.user_id,
                SessionState.WAITING_APPEAL_REASON,
                intent=IntentType.APPEAL,
            )
            await self.qclaw.send_message(
                msg.user_id, "请说明申诉原因:"
            )
            return

        # 找到学生最近的提交
        assignments = self.db.list_assignments()
        for a in assignments:
            sub = self.db.get_student_submission(a.id, msg.user_id)
            if sub and sub.score is not None:
                appeal = self.appeal_handler.submit_appeal(
                    submission_id=sub.id,
                    student_id=msg.user_id,
                    reason=reason,
                )
                # 推送给 TA
                push = self.ta_channel.format_appeal_push(appeal, sub)
                await self.qclaw.send_message(msg.user_id, push)
                return

        await self.qclaw.send_message(
            msg.user_id, "未找到可申诉的作业"
        )

    async def _handle_view_report(self, msg: QQMessage, intent):
        """处理查看报告"""
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(
                msg.user_id, "暂无报告"
            )
            return

        assignment = assignments[-1]
        path = self.report_gen.get_or_generate(assignment.id)
        await self.qclaw.send_message(
            msg.user_id,
            f"报告已生成: {os.path.basename(path)}\n"
            f"路径: {path}"
        )

    async def _handle_ta_action(self, msg: QQMessage, intent):
        """处理 TA 审批"""
        action, note = self.ta_channel.parse_ta_instruction(msg.content)
        pending = self.db.list_pending_appeals()

        if not pending:
            await self.qclaw.send_message(msg.user_id, "当前没有待处理的申诉")
            return

        appeal = pending[0]  # 处理第一个
        approved = (action == "approve")

        result = self.appeal_handler.process_appeal(
            appeal.id, approved=approved, reviewer_id=msg.user_id
        )
        confirm = self.ta_channel.confirm_decision(
            appeal.id, action, note
        )
        await self.qclaw.send_message(msg.user_id, confirm)

    async def _handle_query_progress(self, msg: QQMessage, intent):
        """处理进度查询"""
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(
                msg.user_id, "暂无作业进度"
            )
            return

        report = self.teacher_stats.format_progress_report(assignments[-1].id)
        await self.qclaw.send_message(msg.user_id, report)

    async def _handle_ask_question(self, msg: QQMessage, intent):
        """处理提问（知识库检索）"""
        results = self.knowledge.search(msg.content, top_k=2)
        if results:
            answer = "📚 找到以下相关内容:\n\n"
            for i, r in enumerate(results, 1):
                answer += f"{i}. {r[:200]}...\n\n"
            await self.qclaw.send_message(msg.user_id, answer)
        else:
            await self.qclaw.send_message(
                msg.user_id,
                "抱歉，知识库中没有找到相关内容。"
            )

    # ── 启动 ────────────────────────────────

    async def run(self):
        """启动助手"""
        logger.info(f"Qclaw 模式: {self.qclaw.mode}")
        await self.qclaw.start()
        logger.info("QQ 课程助手已启动")


# ── 入口 ──────────────────────────────────

async def main():
    assistant = QQCourseAssistant()
    await assistant.run()


if __name__ == "__main__":
    asyncio.run(main())
