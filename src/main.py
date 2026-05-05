"""
QQ 课程助手 MVP 主入口
整合所有模块，处理消息循环
"""
from __future__ import annotations
import asyncio
import logging
import sys
import os
from datetime import datetime
from typing import Optional

# 添加 src 到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 .env 环境变量（必须在导入模块之前）
from dotenv import load_dotenv
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_env_path, override=False)
logger_init = logging.getLogger("main")
logger_init.info(f"已加载 .env: {_env_path}")

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
        self.ta_channel = TAChannelManager(
            self.db,
            qclaw_adapter=self.qclaw,
        )
        self.teacher_stats = TeacherStats(self.db)
        self.report_gen = ReportGenerator(self.db, knowledge_base=self.knowledge)
        self.rubric_parser = RubricParser()
        self.file_parser = FileParser()
        self.homework = HomeworkReceiver(self.db)
        self.grading = GradingEngine(self.db)
        self.notifier = ResultNotifier(self.qclaw)
        # 申诉处理器：传入通知回调，审批后自动通知学生
        self.appeal_handler = AppealHandler(
            self.db, self.grading,
            notify_callback=self._notify_student
        )

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
            elif intent.intent == IntentType.GENERATE_STUDENT_REPORT:
                await self._handle_student_report(msg, intent)
            elif intent.intent in (IntentType.TA_APPROVE, IntentType.TA_REJECT):
                await self._handle_ta_action(msg, intent)
            elif intent.intent == IntentType.TA_COMMAND:
                await self._handle_ta_command(msg, intent)
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
        """处理课件上传 — 完整链路"""
        import uuid
        from contracts.models import Courseware

        # 1. 如果消息带有文件附件，直接处理
        if msg.file_url and msg.file_name:
            try:
                save_path = os.path.join(
                    ".data", "courseware",
                    f"{uuid.uuid4().hex[:8]}_{msg.file_name}"
                )
                os.makedirs(os.path.dirname(save_path), exist_ok=True)

                await self.qclaw.download_file(msg.file_url, save_path)

                # 2. 解析文件内容
                parsed = self.file_parser.parse(save_path)
                if not parsed.get("text"):
                    await self.qclaw.send_message(
                        msg.user_id,
                        f"课件解析失败: {parsed.get('error', '无法提取文本')}"
                    )
                    return

                text = parsed["text"]
                title = parsed.get("title") or msg.file_name

                # 3. 存储课件
                courseware = Courseware(
                    id=f"cw_{uuid.uuid4().hex[:8]}",
                    title=title,
                    content=text,
                    file_path=save_path,
                    uploaded_by=msg.user_id,
                    created_at=datetime.now(),
                )
                self.db.save_courseware(courseware)

                # 4. 入库知识库（分块 + 向量化）
                self.knowledge.add_courseware(
                    courseware_id=courseware.id,
                    text=text,
                    metadata={"title": title, "uploaded_by": msg.user_id}
                )

                chunk_count = self.knowledge.get_chunk_count(courseware.id)
                await self.qclaw.send_message(
                    msg.user_id,
                    f"课件上传成功!\n"
                    f"标题: {title}\n"
                    f"课件ID: {courseware.id}\n"
                    f"分块数: {chunk_count}\n"
                    f"字数: {len(text)}"
                )

            except IOError as e:
                await self.qclaw.send_message(
                    msg.user_id, f"文件下载失败: {e}"
                )
            except Exception as e:
                logger.error(f"课件处理异常: {e}", exc_info=True)
                await self.qclaw.send_message(
                    msg.user_id, f"课件处理出错: {e}"
                )
            return

        # 2. 如果消息中有纯文本内容，直接作为课件入库
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
            self.knowledge.add_courseware(
                courseware_id=courseware.id,
                text=content,
            )
            chunk_count = self.knowledge.get_chunk_count(courseware.id)
            await self.qclaw.send_message(
                msg.user_id,
                f"文本课件已入库!\n"
                f"课件ID: {courseware.id}\n"
                f"分块数: {chunk_count}"
            )
            return

        # 3. 无文件也无足够文本，引导用户上传
        await self.qclaw.send_message(
            msg.user_id,
            "请上传课件文件（支持 PDF/DOCX/TXT/MD），\n"
            "或直接发送课件文本内容（至少20字）"
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

        try:
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
        except ValueError as e:
            await self.qclaw.send_message(
                msg.user_id, f"提交失败: {e}"
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
                # 推送给 TA 频道（完整推送，含分配和通知队列）
                await self.ta_channel.push_appeal_to_ta_channel(appeal, sub)
                # 同时给学生确认
                await self.qclaw.send_message(
                    msg.user_id,
                    f"✅ 申诉已提交! 申诉ID: {appeal.id}\n"
                    f"已通知助教处理，请耐心等待。"
                )
                return

        await self.qclaw.send_message(
            msg.user_id, "未找到可申诉的作业"
        )

    async def _handle_view_report(self, msg: QQMessage, intent):
        """处理查看报告（班级汇总，含雷达图+课件依据）"""
        from contracts.models import ReportConfig, ReportType

        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(
                msg.user_id, "暂无报告"
            )
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
            f"📊 班级报告已生成: {os.path.basename(path)}\n"
            f"路径: {path}\n"
            f"含雷达图: ✅ | 含课件依据: ✅"
        )

    async def _handle_student_report(self, msg: QQMessage, intent):
        """处理学生个人报告（雷达图+课件依据+排名）"""
        from contracts.models import ReportConfig, ReportType

        assignments = self.db.list_assignments()
        if not assignments:
            await self.qclaw.send_message(
                msg.user_id, "暂无作业，无法生成个人报告"
            )
            return

        assignment = assignments[-1]
        student_id = msg.user_id

        # 检查学生是否有提交
        submission = self.db.get_student_submission(assignment.id, student_id)
        if not submission:
            await self.qclaw.send_message(
                msg.user_id,
                f"您尚未提交作业「{assignment.title}」\n"
                f"请先提交作业后再查看个人报告"
            )
            return

        config = ReportConfig(
            assignment_id=assignment.id,
            report_type=ReportType.STUDENT_DETAIL,
            student_id=student_id,
            include_radar=True,
            include_evidence=True,
            include_ranking=True,
        )
        path = self.report_gen.generate_with_config(config)
        await self.qclaw.send_message(
            msg.user_id,
            f"👤 个人报告已生成: {os.path.basename(path)}\n"
            f"路径: {path}\n"
            f"含雷达图: ✅ | 含课件依据: ✅ | 含排名: ✅"
        )

    async def _handle_ta_action(self, msg: QQMessage, intent):
        """处理 TA 审批（含角色鉴权）"""
        # 角色鉴权：检查用户是否有 TA/教师权限
        if msg.group_id:
            roles = await self.qclaw.get_member_roles(msg.group_id, msg.user_id)
            if not any(r in ("teacher", "ta") for r in roles):
                await self.qclaw.send_message(
                    msg.user_id, "您没有 TA/教师权限，无法审批申诉"
                )
                return

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

    async def _handle_ta_command(self, msg: QQMessage, intent):
        """处理 TA 工作台指令"""
        from ta_channel.ta_channel import TACommandParser

        # 解析指令
        command, params = TACommandParser.parse(msg.content)

        if command == "unknown":
            await self.qclaw.send_message(
                msg.user_id,
                "⚠️ 无法识别的 TA 指令。输入 /帮助 查看可用指令"
            )
            return

        # 处理指令
        reply = await self.ta_channel.handle_ta_command(
            message=msg,
            command=command,
            params=params,
            appeal_handler=self.appeal_handler,
        )

        if reply:
            await self.qclaw.send_message(msg.user_id, reply)

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

    # ── 通知辅助 ────────────────────────────

    async def _notify_student(self, student_id: str, message: str):
        """通知学生（用于申诉结果等自动通知）"""
        try:
            await self.qclaw.send_message(student_id, message)
        except Exception as e:
            logger.warning(f"通知学生失败: student={student_id}, error={e}")

    # ── 启动 ────────────────────────────────

    async def run(self):
        """启动助手"""
        logger.info(f"Qclaw 模式: {self.qclaw.mode}")
        await self.qclaw.start()

        # 启动通知队列消费者
        notify_queue = self.ta_channel.get_notify_queue()
        if notify_queue:
            notify_queue.start_consumer(loop=asyncio.get_event_loop())
            logger.info("通知队列消费者已启动")

        logger.info("QQ 课程助手已启动")


# ── 入口 ──────────────────────────────────

async def main():
    assistant = QQCourseAssistant()
    await assistant.run()


if __name__ == "__main__":
    asyncio.run(main())
