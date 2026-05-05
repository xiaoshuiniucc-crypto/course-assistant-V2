"""
QQ course assistant main entry.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_env_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".env",
)
load_dotenv(_env_path, override=False)

from appeal.handler import AppealHandler
from contracts.models import (
    Assignment,
    Courseware,
    IntentType,
    QQMessage,
    ReportConfig,
    ReportType,
)
from fileparser.file_parser import FileParser
from grading.engine import GradingEngine
from homework.receiver import HomeworkReceiver
from knowledge.knowledge_base import KnowledgeBase
from notify.result_notifier import ResultNotifier
from qclaw.adapter import QclawAdapter
from report.report_gen import ReportGenerator
from router.router import IntentRouter
from rubric.rubric_parser import RubricParser
from storage.db import DB
from ta_channel.ta_channel import TAChannelManager, TACommandParser
from teacher_stats.stats import TeacherStats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")


class QQCourseAssistant:
    def __init__(self, db_path: str = "qq_course.db"):
        self.db = DB(db_path)
        self.qclaw = QclawAdapter()
        self.router = IntentRouter(self.db)
        self.knowledge = KnowledgeBase(self.db)
        self.ta_channel = TAChannelManager(self.db, qclaw_adapter=self.qclaw)
        self.teacher_stats = TeacherStats(self.db)
        self.report_gen = ReportGenerator(self.db, knowledge_base=self.knowledge)
        self.rubric_parser = RubricParser()
        self.file_parser = FileParser()
        self.homework = HomeworkReceiver(self.db)
        self.grading = GradingEngine(self.db)
        self.notifier = ResultNotifier(self.qclaw)
        self.appeal_handler = AppealHandler(
            self.db,
            self.grading,
            notify_callback=self._notify_student,
        )
        self.qclaw.on_message(self._handle_message)

    async def _handle_message(self, msg: QQMessage):
        try:
            intent = self.router.classify(msg)
            logger.info("Intent: %s (confidence=%.2f)", intent.intent, intent.confidence)

            if intent.intent == IntentType.UPLOAD_COURSEWARE:
                await self._handle_upload_courseware(msg, intent)
            elif intent.intent == IntentType.SET_RUBRIC:
                await self._handle_set_rubric(msg, intent)
            elif intent.intent == IntentType.SET_ASSIGNMENT:
                await self._handle_set_assignment(msg, intent)
            elif intent.intent == IntentType.SUBMIT_HOMEWORK:
                await self._handle_submit_homework(msg)
            elif intent.intent == IntentType.APPEAL:
                await self._handle_appeal(msg, intent)
            elif intent.intent == IntentType.VIEW_REPORT:
                await self._handle_view_report(msg)
            elif intent.intent == IntentType.GENERATE_STUDENT_REPORT:
                await self._handle_student_report(msg)
            elif intent.intent in (IntentType.TA_APPROVE, IntentType.TA_REJECT):
                await self._handle_ta_action(msg)
            elif intent.intent == IntentType.TA_COMMAND:
                await self._handle_ta_command(msg)
            elif intent.intent == IntentType.QUERY_PROGRESS:
                await self._handle_query_progress(msg)
            elif intent.intent == IntentType.ASK_QUESTION:
                await self._handle_ask_question(msg)
            else:
                await self.qclaw.send_message(
                    msg.user_id,
                    "抱歉，我没有理解您的意思。\n"
                    "可以试试：上传课件 / 设置评分标准 / 布置作业 / 提交作业 / 查看报告 / 查询进度",
                )
        except Exception as e:
            logger.error("Message handling failed: %s", e, exc_info=True)
            await self.qclaw.send_message(msg.user_id, f"处理出错: {e}")

    async def _handle_upload_courseware(self, msg: QQMessage, intent):
        file_ref = intent.slots.get("file_path") or msg.file_url
        if not file_ref:
            content = msg.content.replace("上传课件", "").strip()
            if content and len(content) >= 20:
                courseware = Courseware(
                    id=f"cw_{uuid.uuid4().hex[:8]}",
                    title=content[:30] + "...",
                    content=content,
                    file_path="",
                    uploaded_by=msg.user_id,
                    created_at=datetime.now(),
                )
                self.db.save_courseware(courseware)
                self.knowledge.add_courseware(courseware.id, content)
                await self.qclaw.send_message(
                    msg.user_id,
                    f"文本课件已入库\n课件ID: {courseware.id}\n知识块数: {self.knowledge.get_chunk_count(courseware.id)}",
                )
                return

            await self.qclaw.send_message(
                msg.user_id,
                "请发送课件文件，或在 mock 模式中传入 file_path。支持 PDF/DOCX/TXT/MD/CSV。",
            )
            return

        try:
            source_path = await self._prepare_courseware_file(file_ref, msg.file_name)
            content = self.file_parser.parse(source_path)
            if not content.strip():
                raise ValueError("课件解析结果为空")

            courseware = Courseware(
                id=f"cw_{uuid.uuid4().hex[:8]}",
                title=self.file_parser.detect_title(source_path, content),
                content=content,
                file_path=source_path,
                uploaded_by=msg.user_id,
                created_at=datetime.now(),
                meta={
                    "source": "remote" if msg.file_url else "local",
                    "original_name": msg.file_name or Path(source_path).name,
                    "message_id": msg.message_id,
                },
            )
            self.db.save_courseware(courseware)
            self.knowledge.add_courseware(
                courseware.id,
                courseware.content,
                metadata={
                    "title": courseware.title,
                    "uploaded_by": courseware.uploaded_by,
                },
            )
            await self.qclaw.send_message(
                msg.user_id,
                f"课件已入库: {courseware.title}\n"
                f"课件ID: {courseware.id}\n"
                f"文件: {os.path.basename(courseware.file_path)}\n"
                f"知识块数: {self.knowledge.get_chunk_count(courseware.id)}",
            )
        except Exception as e:
            logger.error("Courseware upload failed: %s", e, exc_info=True)
            await self.qclaw.send_message(msg.user_id, f"课件上传失败: {e}")

    async def _handle_set_rubric(self, msg: QQMessage, intent):
        if intent.slots.get("rubric_text"):
            rubric = self.rubric_parser.parse(intent.slots["rubric_text"])
            self.db.save_rubric(rubric)
            await self.qclaw.send_message(
                msg.user_id,
                f"评分标准已设置:\n{self.rubric_parser.format_rubric(rubric)}",
            )
            return

        from router.router import SessionState

        self.router.set_waiting(
            intent.session_id,
            msg.user_id,
            SessionState.WAITING_RUBRIC_TEXT,
            intent=IntentType.SET_RUBRIC,
        )
        await self.qclaw.send_message(
            msg.user_id,
            "请输入评分标准，格式示例：\n"
            "内容准确性:0.4:答案是否正确\n"
            "逻辑性:0.3:推理是否合理\n"
            "表达:0.3:语言是否清晰",
        )

    async def _handle_set_assignment(self, msg: QQMessage, intent):
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
            f"作业已创建: {assignment.title} (ID: {assignment.id})",
        )

    async def _handle_submit_homework(self, msg: QQMessage):
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(msg.user_id, "暂无可提交的作业")
            return

        assignment = assignments[-1]
        content = msg.content.replace("提交作业", "").strip() or "（学生提交内容）"

        try:
            submission = self.homework.receive(
                assignment_id=assignment.id,
                student_id=msg.user_id,
                student_name=f"学生_{msg.user_id[:4]}",
                content=content,
            )
            await self.qclaw.send_message(
                msg.user_id,
                f"作业已提交! 提交ID: {submission.id}\n状态: {submission.status}",
            )
        except ValueError as e:
            await self.qclaw.send_message(msg.user_id, f"提交失败: {e}")

    async def _handle_appeal(self, msg: QQMessage, intent):
        reason = intent.slots.get("appeal_reason", "")
        if not reason:
            from router.router import SessionState

            self.router.set_waiting(
                intent.session_id,
                msg.user_id,
                SessionState.WAITING_APPEAL_REASON,
                intent=IntentType.APPEAL,
            )
            await self.qclaw.send_message(msg.user_id, "请说明申诉原因:")
            return

        assignments = self.db.list_assignments()
        for assignment in assignments:
            submission = self.db.get_student_submission(assignment.id, msg.user_id)
            if submission and submission.score is not None:
                try:
                    appeal = self.appeal_handler.submit_appeal(
                        submission_id=submission.id,
                        student_id=msg.user_id,
                        reason=reason,
                    )
                except ValueError as e:
                    await self.qclaw.send_message(msg.user_id, str(e))
                    return

                await self.ta_channel.push_appeal_to_ta_channel(appeal, submission)
                await self.qclaw.send_message(
                    msg.user_id,
                    f"申诉已提交，等待 TA/教师处理。\n申诉ID: {appeal.id}",
                )
                return

        await self.qclaw.send_message(msg.user_id, "未找到可申诉的作业")

    async def _handle_view_report(self, msg: QQMessage):
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(msg.user_id, "暂无报告")
            return

        assignment = assignments[-1]
        config = ReportConfig(
            assignment_id=assignment.id,
            report_type=ReportType.CLASS_SUMMARY,
            include_radar=True,
            include_evidence=True,
        )
        path = self.report_gen.generate_with_config(config)
        await self.qclaw.send_message(
            msg.user_id,
            f"班级报告已生成: {os.path.basename(path)}\n路径: {path}",
        )

    async def _handle_student_report(self, msg: QQMessage):
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(msg.user_id, "暂无作业，无法生成个人报告")
            return

        assignment = assignments[-1]
        submission = self.db.get_student_submission(assignment.id, msg.user_id)
        if not submission:
            await self.qclaw.send_message(
                msg.user_id,
                f"您尚未提交作业《{assignment.title}》，请先提交后再查看个人报告。",
            )
            return

        config = ReportConfig(
            assignment_id=assignment.id,
            report_type=ReportType.STUDENT_DETAIL,
            student_id=msg.user_id,
            include_radar=True,
            include_evidence=True,
            include_ranking=True,
        )
        path = self.report_gen.generate_with_config(config)
        await self.qclaw.send_message(
            msg.user_id,
            f"个人报告已生成: {os.path.basename(path)}\n路径: {path}",
        )

    async def _handle_ta_action(self, msg: QQMessage):
        roles = await self.qclaw.get_member_roles(msg.group_id or "", msg.user_id)
        if "ta" not in roles and "teacher" not in roles:
            await self.qclaw.send_message(msg.user_id, "当前账号没有 TA/教师审批权限。")
            return

        action, note = self.ta_channel.parse_ta_instruction(msg.content)
        if action == "unknown":
            await self.qclaw.send_message(msg.user_id, "无法识别审批指令，请回复“同意”或“驳回”。")
            return

        pending = self.db.list_pending_appeals()
        if not pending:
            await self.qclaw.send_message(msg.user_id, "当前没有待处理的申诉")
            return

        appeal = pending[0]
        approved = action == "approve"
        self.appeal_handler.process_appeal(
            appeal.id,
            approved=approved,
            reviewer_id=msg.user_id,
        )
        confirm = self.ta_channel.confirm_decision(appeal.id, action, note)
        await self.qclaw.send_message(msg.user_id, confirm)

    async def _handle_ta_command(self, msg: QQMessage):
        command, params = TACommandParser.parse(msg.content)
        if command == "unknown":
            await self.qclaw.send_message(msg.user_id, "无法识别的 TA 指令，输入 /帮助 查看可用指令")
            return

        reply = await self.ta_channel.handle_ta_command(
            message=msg,
            command=command,
            params=params,
            appeal_handler=self.appeal_handler,
        )
        if reply:
            await self.qclaw.send_message(msg.user_id, reply)

    async def _handle_query_progress(self, msg: QQMessage):
        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(msg.user_id, "暂无作业进度")
            return

        report = self.teacher_stats.format_progress_report(assignments[-1].id)
        await self.qclaw.send_message(msg.user_id, report)

    async def _handle_ask_question(self, msg: QQMessage):
        results = self.knowledge.search(msg.content, top_k=2)
        if results:
            answer = "找到以下相关内容:\n\n"
            for index, item in enumerate(results, 1):
                answer += f"{index}. {item[:200]}...\n\n"
            await self.qclaw.send_message(msg.user_id, answer)
            return

        await self.qclaw.send_message(msg.user_id, "抱歉，知识库中没有找到相关内容。")

    async def _notify_student(self, student_id: str, message: str):
        try:
            await self.qclaw.send_message(student_id, message)
        except Exception as e:
            logger.warning("Failed to notify student=%s: %s", student_id, e)

    async def _prepare_courseware_file(self, file_ref: str, file_name: str | None) -> str:
        uploads_dir = Path("data") / "uploads" / "courseware"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        if file_ref.startswith(("http://", "https://")):
            suffix = Path(file_name or file_ref).suffix or ".bin"
            target_path = uploads_dir / f"{uuid.uuid4().hex[:8]}{suffix}"
            await self.qclaw.download_file(file_ref, str(target_path))
            return str(target_path)

        candidate = Path(file_ref)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.exists():
            raise FileNotFoundError(f"找不到课件文件: {candidate}")
        return str(candidate)

    async def run(self):
        logger.info("Qclaw mode: %s", self.qclaw.mode)
        await self.qclaw.start()

        notify_queue = self.ta_channel.get_notify_queue()
        if notify_queue:
            notify_queue.start_consumer(loop=asyncio.get_event_loop())
            logger.info("Notification queue consumer started")

        logger.info("QQ course assistant started")


async def main():
    assistant = QQCourseAssistant()
    await assistant.run()


if __name__ == "__main__":
    asyncio.run(main())
