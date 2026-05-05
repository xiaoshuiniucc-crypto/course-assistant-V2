"""
作业接收器
接收学生提交的作业，存储到数据库
"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional

from contracts.models import HomeworkSubmission, SubmitStatus
from storage.db import DB


class HomeworkReceiver:
    """作业提交管理（含截止日期校验）"""

    def __init__(self, db: DB):
        self.db = db

    def receive(
        self,
        assignment_id: str,
        student_id: str,
        student_name: str,
        content: str,
        file_path: Optional[str] = None,
    ) -> HomeworkSubmission:
        """接收一份作业提交

        Returns:
            HomeworkSubmission: 提交对象

        Raises:
            ValueError: 作业不存在或已过截止日期
        """
        # 检查作业是否存在
        assignment = self.db.get_assignment(assignment_id)
        if not assignment:
            raise ValueError(f"作业不存在: {assignment_id}")

        # 检查截止日期
        if assignment.deadline and datetime.now() > assignment.deadline:
            raise ValueError(
                f"作业已过截止日期 ({assignment.deadline.strftime('%Y-%m-%d %H:%M')})"
            )

        # 检查是否已提交
        existing = self.db.get_student_submission(assignment_id, student_id)
        if existing and existing.status in (
            SubmitStatus.SUBMITTED, SubmitStatus.GRADED, SubmitStatus.FINAL
        ):
            # 已提交且已评分 → 不能重复提交
            return existing

        submission = HomeworkSubmission(
            id=f"sub_{uuid.uuid4().hex[:8]}",
            assignment_id=assignment_id,
            student_id=student_id,
            student_name=student_name,
            content=content,
            file_path=file_path,
            status=SubmitStatus.SUBMITTED,
            submitted_at=datetime.now(),
        )
        self.db.save_submission(submission)
        return submission

    def get_submission(self, submission_id: str) -> Optional[HomeworkSubmission]:
        return self.db.get_submission(submission_id)

    def list_submissions(self, assignment_id: str):
        return self.db.list_submissions(assignment_id)

    def update_after_grading(self, submission: HomeworkSubmission):
        """批改后更新提交状态"""
        submission.status = SubmitStatus.GRADED
        self.db.save_submission(submission)
