"""
QQ course assistant main entry.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
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
    SubmitStatus,
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
from runtime.single_instance import SingleInstanceError, SingleInstanceGuard
from storage.db import DB
from ta_channel.ta_channel import TAChannelManager, TACommandParser
from teacher_stats.stats import TeacherStats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
        self._recent_message_keys = {}
        self._message_dedupe_ttl_seconds = 15.0
        self._stop_event = asyncio.Event()
        self.qclaw.on_message(self._handle_message)

    def _resolve_course_id(self, msg: QQMessage) -> str:
        if msg.group_id:
            bound_course = self.db.get_course_id_by_group(msg.group_id)
            if bound_course:
                return bound_course
            inferred_course = msg.group_id.replace("group_", "").strip() or "default"
            self.db.bind_group_to_course(inferred_course, msg.group_id, msg.group_id)
            return inferred_course
        return "default"

    def _get_course_rubric(self, assignment: Assignment, course_id: str):
        if assignment.rubric_id:
            rubric = self.db.get_rubric(assignment.rubric_id)
            if rubric:
                return rubric
        return self._get_latest_course_rubric(course_id)

    def _get_latest_course_rubric(self, course_id: str):
        row = self.db._conn.execute(
            "SELECT id FROM rubrics WHERE course_id=? ORDER BY created_at DESC LIMIT 1",
            (course_id,),
        ).fetchone()
        if not row:
            return None
        return self.db.get_rubric(row["id"])

    def _build_session_id(self, msg: QQMessage) -> str:
        if msg.group_id:
            return f"sess_{msg.user_id}_{msg.group_id}"
        return f"sess_{msg.user_id}"

    async def _handle_message(self, msg: QQMessage):
        try:
            if not self._should_process_message(msg):
                logger.info(
                    "Skip duplicate message: id=%s user=%s type=%s",
                    msg.message_id,
                    msg.user_id,
                    msg.message_type,
                )
                return

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
                await self._handle_unknown_message(msg)
        except Exception as e:
            logger.error("Message handling failed: %s", e, exc_info=True)
            await self.qclaw.send_message(
                msg.user_id,
                "刚才这条消息处理失败了，我已经记录日志。请重试一次；如果还是不行，我再继续排查。",
            )

    def _should_process_message(self, msg: QQMessage) -> bool:
        now = datetime.now().timestamp()
        expire_before = now - self._message_dedupe_ttl_seconds
        stale_keys = [
            key for key, last_seen in self._recent_message_keys.items()
            if last_seen < expire_before
        ]
        for key in stale_keys:
            self._recent_message_keys.pop(key, None)

        dedupe_key = self._build_message_dedupe_key(msg)
        if dedupe_key in self._recent_message_keys:
            return False

        self._recent_message_keys[dedupe_key] = now
        return True

    def _build_message_dedupe_key(self, msg: QQMessage) -> str:
        if msg.message_id:
            return f"id:{msg.message_type}:{msg.message_id}"

        normalized_content = re.sub(r"\s+", " ", (msg.content or "").strip())
        file_part = msg.file_url or msg.file_name or ""
        group_part = msg.group_id or ""
        return (
            f"fallback:{msg.message_type}:{msg.user_id}:{group_part}:"
            f"{normalized_content}:{file_part}"
        )

    async def _handle_upload_courseware(self, msg: QQMessage, intent):
        course_id = self._resolve_course_id(msg)
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
                    course_id=course_id,
                )
                self.db.save_courseware(courseware)
                self.knowledge.add_courseware(courseware.id, content, course_id=course_id)
                await self.qclaw.send_message(
                    msg.user_id,
                    f"文本课件已入库\n课程: {course_id}\n课件ID: {courseware.id}\n知识块数: {self.knowledge.get_chunk_count(courseware.id)}",
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
                course_id=course_id,
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
                course_id=course_id,
            )
            await self.qclaw.send_message(
                msg.user_id,
                f"课件已入库: {courseware.title}\n"
                f"课程: {course_id}\n"
                f"课件ID: {courseware.id}\n"
                f"文件: {os.path.basename(courseware.file_path)}\n"
                f"知识块数: {self.knowledge.get_chunk_count(courseware.id)}",
            )
        except Exception as e:
            logger.error("Courseware upload failed: %s", e, exc_info=True)
            await self.qclaw.send_message(msg.user_id, f"课件上传失败: {e}")

    async def _handle_set_rubric(self, msg: QQMessage, intent):
        course_id = self._resolve_course_id(msg)
        if intent.waiting_for == "waiting_rubric_approval":
            await self._handle_rubric_approval(msg, intent, course_id)
            return

        if intent.slots.get("rubric_text"):
            rubric_text = intent.slots["rubric_text"].strip()
            if not self._looks_like_rubric_text(rubric_text):
                await self.qclaw.send_message(
                    msg.user_id,
                    (
                        "\u8fd9\u6761\u5185\u5bb9\u770b\u8d77\u6765\u4e0d\u50cf\u8bc4\u5206\u6807\u51c6\uff0c"
                        "\u8bf7\u6309\u201c\u7ef4\u5ea6:\u6743\u91cd:\u8bf4\u660e\u201d\u7684\u683c\u5f0f\u53d1\u9001\u3002"
                    ),
                )
                return

            rubric = self.rubric_parser.parse(rubric_text)
            rubric.course_id = course_id
            self.db.save_rubric(rubric)
            await self.qclaw.send_message(
                msg.user_id,
                (
                    f"\u8bc4\u5206\u6807\u51c6\u5df2\u8bbe\u7f6e(\u8bfe\u7a0b: {course_id})\n"
                    f"{self.rubric_parser.format_rubric(rubric)}"
                ),
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
            "\u8bf7\u8f93\u5165\u8bc4\u5206\u6807\u51c6\uff0c\u683c\u5f0f\u793a\u4f8b\uff1a\n"
            "\u5185\u5bb9\u51c6\u786e\u6027:0.4:\u7b54\u6848\u662f\u5426\u6b63\u786e\n"
            "\u903b\u8f91\u6027:0.3:\u63a8\u7406\u662f\u5426\u5408\u7406\n"
            "\u8868\u8fbe:0.3:\u8bed\u8a00\u662f\u5426\u6e05\u6670",
        )

    async def _handle_set_assignment(self, msg: QQMessage, intent):
        course_id = self._resolve_course_id(msg)
        assignment_title = self._extract_assignment_title(msg.content)
        assignment_requirement = self._extract_assignment_requirement(msg.content, assignment_title)
        session_id = self._build_session_id(msg)

        try:
            draft = self.grading.generate_rubric_draft(
                assignment_title=assignment_title,
                assignment_requirement=assignment_requirement,
                course_id=course_id,
            )
        except Exception as e:
            logger.error("Rubric draft generation failed: %s", e, exc_info=True)
            await self.qclaw.send_message(
                msg.user_id,
                (
                    f"作业要求已收到，但 AI 生成评分标准草案失败：{e}\n"
                    "你可以直接发送评分标准文本，或稍后重新发送“布置作业 + 要求”。"
                ),
            )
            return

        draft_path = self.grading.write_rubric_json(
            draft,
            stage="draft",
            course_id=course_id,
            assignment_title=assignment_title,
            assignment_requirement=assignment_requirement,
        )
        from router.router import SessionState

        self.router.set_waiting(
            session_id,
            msg.user_id,
            SessionState.WAITING_RUBRIC_APPROVAL,
            intent=IntentType.SET_RUBRIC,
            slots={
                "assignment_title": assignment_title,
                "assignment_requirement": assignment_requirement,
                "rubric_draft": draft,
                "rubric_draft_path": draft_path,
                "course_id": course_id,
            },
        )
        await self.qclaw.send_message(
            msg.user_id,
            self._format_rubric_draft_message(assignment_title, course_id, draft, draft_path),
        )

    async def _handle_rubric_approval(self, msg: QQMessage, intent, course_id: str):
        slots = intent.slots or {}
        draft = slots.get("rubric_draft")
        assignment_title = slots.get("assignment_title") or "新作业"
        assignment_requirement = slots.get("assignment_requirement") or assignment_title
        session_id = intent.session_id or self._build_session_id(msg)
        teacher_reply = (slots.get("teacher_reply") or msg.content or "").strip()

        if not draft:
            self.router.set_waiting(session_id, msg.user_id, None, intent=None, slots={})
            await self.qclaw.send_message(
                msg.user_id,
                "当前没有待确认的评分标准草案，请重新发送“布置作业 + 作业要求”。",
            )
            return

        if self._is_rubric_confirm_reply(teacher_reply):
            rubric = self.grading.rubric_from_payload(draft, course_id=course_id)
            self.db.save_rubric(rubric)
            assignment = Assignment(
                id=f"asgn_{uuid.uuid4().hex[:8]}",
                title=assignment_title,
                description=assignment_requirement,
                rubric_id=rubric.id,
                courseware_id=None,
                deadline=None,
                created_at=datetime.now(),
                course_id=course_id,
            )
            self.db.save_assignment(assignment)
            final_path = self.grading.write_rubric_json(
                draft,
                stage="confirmed",
                course_id=course_id,
                assignment_title=assignment_title,
                assignment_requirement=assignment_requirement,
                rubric_id=rubric.id,
                assignment_id=assignment.id,
            )
            self.router.set_waiting(session_id, msg.user_id, None, intent=None, slots={})
            await self.qclaw.send_message(
                msg.user_id,
                (
                    f"评分标准已确认并保存。\n"
                    f"作业已创建：{assignment.title} (ID: {assignment.id}, 课程: {course_id})\n"
                    f"评分标准ID: {rubric.id}\n"
                    f"JSON记录: {final_path}\n"
                    f"{self.rubric_parser.format_rubric(rubric)}"
                ),
            )
            return

        feedback = self._normalize_rubric_feedback(teacher_reply)
        try:
            revised = self.grading.generate_rubric_draft(
                assignment_title=assignment_title,
                assignment_requirement=assignment_requirement,
                course_id=course_id,
                teacher_feedback=feedback,
                previous_draft=draft,
            )
        except Exception as e:
            logger.error("Rubric draft revision failed: %s", e, exc_info=True)
            await self.qclaw.send_message(
                msg.user_id,
                (
                    f"已收到修改意见，但 AI 重新生成评分标准失败：{e}\n"
                    "请继续发送更明确的修改意见，或稍后重试。"
                ),
            )
            return

        revised_path = self.grading.write_rubric_json(
            revised,
            stage="revised",
            course_id=course_id,
            assignment_title=assignment_title,
            assignment_requirement=assignment_requirement,
            teacher_feedback=feedback,
        )
        from router.router import SessionState

        self.router.set_waiting(
            session_id,
            msg.user_id,
            SessionState.WAITING_RUBRIC_APPROVAL,
            intent=IntentType.SET_RUBRIC,
            slots={
                "assignment_title": assignment_title,
                "assignment_requirement": assignment_requirement,
                "rubric_draft": revised,
                "rubric_draft_path": revised_path,
                "course_id": course_id,
            },
        )
        await self.qclaw.send_message(
            msg.user_id,
            self._format_rubric_draft_message(
                assignment_title,
                course_id,
                revised,
                revised_path,
                teacher_feedback=feedback,
            ),
        )

    def _looks_like_rubric_text(self, text: str) -> bool:
        if not text:
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return False

        structured_lines = 0
        for line in lines:
            if ":" in line or "?" in line:
                structured_lines += 1

        if structured_lines >= 1:
            return True
        return len(lines) >= 2

    def _extract_assignment_title(self, content: str) -> str:
        text = (content or "").strip()
        prefixes = ("布置作业", "发布作业", "设置作业", "新建作业")
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):].strip(" ：:")
                break
        if not text:
            return "新作业"
        first_line = text.splitlines()[0].strip()
        return first_line[:80] or "新作业"

    def _extract_assignment_requirement(self, content: str, assignment_title: str) -> str:
        text = (content or "").strip()
        requirement = re.sub(r"^(布置作业|发布作业|设置作业|新建作业)\s*", "", text, count=1).strip()
        return requirement or assignment_title

    def _is_rubric_confirm_reply(self, text: str) -> bool:
        normalized = (text or "").strip().lower()
        return normalized in {
            "确认",
            "确认评分标准",
            "确认标准",
            "采用这个评分标准",
            "可以",
            "ok",
            "yes",
        }

    def _normalize_rubric_feedback(self, text: str) -> str:
        normalized = (text or "").strip()
        if normalized.startswith("修改"):
            return normalized
        if normalized.startswith("请修改"):
            return normalized
        return f"请根据以下教师反馈修改评分标准：{normalized}"

    def _format_rubric_draft_message(
        self,
        assignment_title: str,
        course_id: str,
        draft: dict,
        draft_path: str,
        teacher_feedback: str = "",
    ) -> str:
        lines = [
            f"已为作业《{assignment_title}》生成评分标准草案（课程: {course_id}）。",
        ]
        if draft.get("summary"):
            lines.append(f"说明：{draft['summary']}")
        if teacher_feedback:
            lines.append(f"本轮已纳入教师反馈：{teacher_feedback}")
        lines.append("")
        lines.append(f"标题：{draft.get('title', f'{assignment_title}评分标准')}")
        for index, dimension in enumerate(draft.get("dimensions", []), 1):
            weight = float(dimension.get("weight", 0.0))
            lines.append(
                f"{index}. {dimension.get('name', '评分维度')}（权重 {weight:.0%}）: "
                f"{dimension.get('description', '').strip()}"
            )
            focus = dimension.get("grading_focus") or []
            if focus:
                lines.append(f"   关注点：{'、'.join(str(item) for item in focus)}")
        hard_rules = draft.get("hard_rules") or []
        if hard_rules:
            lines.append("硬性规则：" + "；".join(str(rule) for rule in hard_rules))
        lines.append("")
        lines.append("如认可，请回复：确认评分标准")
        lines.append("如需修改，请直接回复修改意见，我会据此重新生成。")
        lines.append(f"JSON草案: {draft_path}")
        return "\n".join(lines)

    async def _handle_submit_homework(self, msg: QQMessage):
        course_id = self._resolve_course_id(msg)
        assignments = self.db.list_assignments(course_id)
        if not assignments:
            await self.qclaw.send_message(
                msg.user_id,
                "\u6682\u65e0\u53ef\u63d0\u4ea4\u7684\u4f5c\u4e1a",
            )
            return

        assignment = assignments[-1]
        existing_submission = self.db.get_student_submission(
            assignment.id,
            msg.user_id,
            course_id,
        )
        if existing_submission and existing_submission.status in (
            SubmitStatus.SUBMITTED,
            SubmitStatus.GRADED,
            SubmitStatus.FINAL,
        ):
            await self.qclaw.send_message(
                msg.user_id,
                (
                    f"\u60a8\u5df2\u7ecf\u63d0\u4ea4\u8fc7\u5f53\u524d\u4f5c\u4e1a\uff1a"
                    f"{assignment.title}\n\u63d0\u4ea4ID: {existing_submission.id}"
                ),
            )
            return

        content = self._extract_submission_text(msg.content)
        file_path = None

        if msg.file_url:
            try:
                file_path = await self._prepare_submission_file(msg.file_url, msg.file_name)
                parsed_content = self.file_parser.parse(file_path).strip()
                if parsed_content:
                    content = f"{content}\n\n{parsed_content}" if content else parsed_content
            except Exception as e:
                logger.error("Submission file preparation failed: %s", e, exc_info=True)
                await self.qclaw.send_message(
                    msg.user_id,
                    f"\u4f5c\u4e1a\u6587\u4ef6\u5904\u7406\u5931\u8d25: {e}",
                )
                return

        if not content:
            await self.qclaw.send_message(
                msg.user_id,
                (
                    "\u8bf7\u53d1\u9001\u4f5c\u4e1a\u6587\u4ef6\uff0c\u6216\u5728"
                    "\u201c\u63d0\u4ea4\u4f5c\u4e1a\u201d\u540e\u9644\u4e0a\u4f5c\u4e1a\u6b63\u6587\u3002"
                ),
            )
            return

        try:
            submission = self.homework.receive(
                assignment_id=assignment.id,
                student_id=msg.user_id,
                student_name=f"\u5b66\u751f_{msg.user_id[:4]}",
                content=content,
                file_path=file_path,
                course_id=course_id,
            )
            file_note = f"\n\u6587\u4ef6: {Path(file_path).name}" if file_path else ""
            await self.qclaw.send_message(
                msg.user_id,
                (
                    f"\u4f5c\u4e1a\u5df2\u63d0\u4ea4\n\u63d0\u4ea4ID: {submission.id}\n"
                    f"\u8bfe\u7a0b: {course_id}\n\u72b6\u6001: {submission.status}{file_note}"
                ),
            )

            rubric = self._get_course_rubric(assignment, course_id)
            if rubric:
                result = self.grading.grade(submission, rubric, assignment.title)
                await self.notifier.notify_student(submission, result, rubric)
        except ValueError as e:
            await self.qclaw.send_message(
                msg.user_id,
                f"\u63d0\u4ea4\u5931\u8d25: {e}",
            )

    def _extract_submission_text(self, raw_content: str) -> str:
        content = (raw_content or "").strip()
        if not content:
            return ""

        patterns = (
            r"\u63d0\u4ea4\u4f5c\u4e1a[:\uff1a]?\s*",
            r"\u4ea4\u4f5c\u4e1a[:\uff1a]?\s*",
            r"\u4f5c\u4e1a\u63d0\u4ea4[:\uff1a]?\s*",
        )
        for pattern in patterns:
            content = re.sub(pattern, "", content, count=1, flags=re.IGNORECASE).strip()
        return content

    def _looks_like_rubric_text(self, text: str) -> bool:
        if not text:
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return False

        structured_lines = 0
        for line in lines:
            if ":" in line or "?" in line:
                structured_lines += 1

        if structured_lines >= 1:
            return True
        return len(lines) >= 2

    async def _handle_appeal(self, msg: QQMessage, intent):
        course_id = self._resolve_course_id(msg)
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

        assignments = self.db.list_assignments(course_id)
        for assignment in assignments:
            submission = self.db.get_student_submission(assignment.id, msg.user_id, course_id)
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
        course_id = self._resolve_course_id(msg)
        assignments = self.db.list_assignments(course_id)
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
        course_id = self._resolve_course_id(msg)
        assignments = self.db.list_assignments(course_id)
        if not assignments:
            await self.qclaw.send_message(msg.user_id, "暂无作业，无法生成个人报告")
            return

        assignment = assignments[-1]
        submission = self.db.get_student_submission(assignment.id, msg.user_id, course_id)
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
        course_id = self._resolve_course_id(msg)
        assignments = self.db.list_assignments(course_id)
        if not assignments:
            await self.qclaw.send_message(msg.user_id, "暂无作业进度")
            return

        report = self.teacher_stats.format_progress_report(assignments[-1].id)
        await self.qclaw.send_message(msg.user_id, report)

    async def _handle_ask_question(self, msg: QQMessage):
        course_id = self._resolve_course_id(msg)
        results = self.knowledge.search_results(msg.content, top_k=3, course_id=course_id)
        ai_answer = self.grading.answer_question(
            question=msg.content,
            context_results=results,
            course_id=course_id,
        )
        if ai_answer:
            await self.qclaw.send_message(msg.user_id, ai_answer)
            return

        if results:
            answer = "找到以下相关内容:\n\n"
            for index, item in enumerate(results, 1):
                answer += f"{index}. {item.as_reference_text()}\n"
                answer += f"   相关度: {item.score:.2f}\n\n"
            await self.qclaw.send_message(msg.user_id, answer)
            return

        await self.qclaw.send_message(
            msg.user_id,
            "抱歉，我暂时没从已入库课件中找到直接相关内容，也还没能生成有效回答。",
        )

    async def _handle_unknown_message(self, msg: QQMessage):
        course_id = self._resolve_course_id(msg)
        results = self.knowledge.search_results(msg.content, top_k=3, course_id=course_id)
        ai_answer = self.grading.answer_question(
            question=msg.content,
            context_results=results,
            course_id=course_id,
        )
        if ai_answer:
            await self.qclaw.send_message(msg.user_id, ai_answer)
            return

        await self.qclaw.send_message(
            msg.user_id,
            "抱歉，我没有理解您的意思。\n"
            "可以试试：上传课件 / 设置评分标准 / 布置作业 / 提交作业 / 查看报告 / 查询进度",
        )

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

    async def _prepare_submission_file(self, file_ref: str, file_name: str | None) -> str:
        uploads_dir = Path("data") / "uploads" / "submissions"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(file_name or file_ref).suffix or ".bin"
        target_path = uploads_dir / f"{uuid.uuid4().hex[:8]}{suffix}"

        if file_ref.startswith(("http://", "https://")):
            await self.qclaw.download_file(file_ref, str(target_path))
            return str(target_path)

        candidate = Path(file_ref)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.exists():
            raise FileNotFoundError(f"找不到作业文件: {candidate}")

        if candidate.resolve() == target_path.resolve():
            return str(candidate)

        target_path.write_bytes(candidate.read_bytes())
        return str(target_path)

    async def run(self):
        logger.info("Qclaw mode: %s", self.qclaw.mode)
        await self.qclaw.start()

        notify_queue = self.ta_channel.get_notify_queue()
        if notify_queue:
            notify_queue.start_consumer(loop=asyncio.get_event_loop())
            logger.info("Notification queue consumer started")

        logger.info("QQ course assistant started")
        await self._stop_event.wait()


async def main():
    assistant = QQCourseAssistant()
    await assistant.run()


if __name__ == "__main__":
    guard = SingleInstanceGuard(PROJECT_ROOT)
    try:
        guard.acquire(
            extra={
                "qq_appid": os.environ.get("QQ_APPID", ""),
                "sandbox": os.environ.get("QQ_SANDBOX", "0") == "1",
            }
        )
    except SingleInstanceError as exc:
        logger.error("Service start blocked by single-instance guard: %s", exc)
        sys.exit(1)

    try:
        asyncio.run(main())
    finally:
        guard.release()
