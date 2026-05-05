"""
申诉处理器
处理学生对批改结果的申诉，支持审批后自动通知
"""
from __future__ import annotations
import uuid
import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable

from contracts.models import Appeal, HomeworkSubmission, GradingResult, SubmitStatus
from storage.db import DB
from grading.engine import GradingEngine

logger = logging.getLogger("appeal")


class AppealHandler:
    """申诉处理（含审批后自动通知）"""

    def __init__(self, db: DB, grading_engine: GradingEngine,
                 notify_callback: Optional[Callable] = None):
        """
        Args:
            db: 数据库
            grading_engine: 批改引擎
            notify_callback: 审批完成后的通知回调
                签名: async callback(student_id: str, message: str) -> None
        """
        self.db = db
        self.grading_engine = grading_engine
        self._notify_callback = notify_callback

    def submit_appeal(self, submission_id: str,
                      student_id: str,
                      reason: str) -> Appeal:
        """学生提交申诉"""
        submission = self.db.get_submission(submission_id)
        if not submission:
            raise ValueError(f"提交不存在: {submission_id}")

        if submission.student_id != student_id:
            raise ValueError("只能对自己的作业申诉")

        if submission.appeal_status == "pending":
            raise ValueError("已有申诉正在处理中")

        # 最多申诉 2 次
        existing = self.db.get_grading_result(submission_id)
        if existing and existing.appeal_count >= 2:
            raise ValueError("已达到最大申诉次数（2次）")

        appeal = Appeal(
            id=f"appeal_{uuid.uuid4().hex[:8]}",
            submission_id=submission_id,
            student_id=student_id,
            reason=reason,
            created_at=datetime.now(),
            status="pending",
        )
        self.db.save_appeal(appeal)

        # 更新提交状态
        submission.appeal_status = "pending"
        self.db.save_submission(submission)

        return appeal

    def process_appeal(self, appeal_id: str,
                       approved: bool,
                       reviewer_id: str = "ta") -> Appeal:
        """处理申诉（TA/教师批准或驳回）"""
        appeal = self.db.get_appeal(appeal_id)
        if not appeal:
            raise ValueError(f"申诉不存在: {appeal_id}")
        if appeal.status != "pending":
            raise ValueError(f"申诉已处理: {appeal.status}")

        appeal.reviewed_by = reviewer_id

        if approved:
            appeal.status = "approved"
            # 申诉重批
            submission = self.db.get_submission(appeal.submission_id)
            if submission:
                assignment = self.db.get_assignment(submission.assignment_id)
                rubric = None
                if assignment and assignment.rubric_id:
                    rubric = self.db.get_rubric(assignment.rubric_id)

                if rubric:
                    title = assignment.title if assignment else ""
                    new_result = self.grading_engine.regrade_for_appeal(
                        submission, rubric, appeal.reason, title
                    )
                    appeal.new_score = new_result.total_score

                    # 更新提交
                    submission.score = new_result.total_score
                    submission.feedback = new_result.feedback
                    submission.appeal_status = "approved"
                    self.db.save_submission(submission)
        else:
            appeal.status = "rejected"
            submission = self.db.get_submission(appeal.submission_id)
            if submission:
                submission.appeal_status = "rejected"
                self.db.save_submission(submission)

        self.db.save_appeal(appeal)

        # 自动通知学生审批结果
        if self._notify_callback and submission:
            try:
                if approved:
                    new_score = appeal.new_score or submission.score
                    notify_msg = (
                        f"您的申诉已批准!\n"
                        f"新分数: {new_score}分\n"
                        f"审核人: {reviewer_id}"
                    )
                else:
                    notify_msg = (
                        f"您的申诉已驳回。\n"
                        f"原分数维持不变 ({submission.score}分)\n"
                        f"审核人: {reviewer_id}"
                    )
                self._notify_callback(submission.student_id, notify_msg)
                logger.info(f"申诉通知已发送: student={submission.student_id}")
            except Exception as e:
                logger.warning(f"申诉通知发送失败: {e}")

        return appeal

    def list_pending(self):
        return self.db.list_pending_appeals()
