"""
Appeal handling.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import datetime
from typing import Callable, Optional

from grading.engine import GradingEngine
from storage.db import DB
from contracts.models import Appeal, GradingResult

logger = logging.getLogger("appeal")


class AppealHandler:
    """Handle student appeals and TA/teacher review."""

    APPEAL_WINDOW_HOURS = 48

    def __init__(
        self,
        db: DB,
        grading_engine: GradingEngine,
        notify_callback: Optional[Callable] = None,
    ):
        self.db = db
        self.grading_engine = grading_engine
        self._notify_callback = notify_callback

    def submit_appeal(self, submission_id: str, student_id: str, reason: str) -> Appeal:
        submission = self.db.get_submission(submission_id)
        if not submission:
            raise ValueError(f"Submission not found: {submission_id}")
        if submission.student_id != student_id:
            raise ValueError("You can only appeal your own submission")
        if submission.appeal_status == "pending":
            raise ValueError("An appeal is already pending for this submission")
        if submission.graded_at is None:
            raise ValueError("This submission has not been graded yet")
        if (datetime.now() - submission.graded_at).total_seconds() > self.APPEAL_WINDOW_HOURS * 3600:
            raise ValueError("The 48-hour appeal window has expired")

        existing = self.db.get_grading_result(submission_id)
        if existing and existing.appeal_count >= 2:
            raise ValueError("Maximum appeal count reached (2)")

        appeal = Appeal(
            id=f"appeal_{uuid.uuid4().hex[:8]}",
            submission_id=submission_id,
            student_id=student_id,
            reason=reason,
            created_at=datetime.now(),
            status="pending",
        )

        assignment = self.db.get_assignment(submission.assignment_id)
        rubric = self.db.get_rubric(assignment.rubric_id) if assignment and assignment.rubric_id else None
        if rubric:
            title = assignment.title if assignment else ""
            regrade_result = self.grading_engine.regrade_for_appeal(
                submission,
                rubric,
                reason,
                title,
            )
            appeal.new_score = regrade_result.total_score
            appeal.ai_feedback = regrade_result.feedback
            appeal.ai_confidence = regrade_result.confidence
            appeal.ai_confidence_label = regrade_result.confidence_label
            appeal.regrade_result = self._serialize_regrade_result(regrade_result)

        self.db.save_appeal(appeal)

        submission.appeal_status = "pending"
        self.db.save_submission(submission)
        return appeal

    def process_appeal(self, appeal_id: str, approved: bool, reviewer_id: str = "ta") -> Appeal:
        appeal = self.db.get_appeal(appeal_id)
        if not appeal:
            raise ValueError(f"Appeal not found: {appeal_id}")
        if appeal.status != "pending":
            raise ValueError(f"Appeal already processed: {appeal.status}")

        submission = self.db.get_submission(appeal.submission_id)
        if not submission:
            raise ValueError(f"Submission not found for appeal: {appeal.submission_id}")

        appeal.reviewed_by = reviewer_id

        if approved:
            appeal.status = "approved"
            if appeal.new_score is not None:
                submission.score = appeal.new_score
            if appeal.ai_feedback:
                submission.feedback = appeal.ai_feedback
            submission.appeal_status = "approved"
            self.db.save_submission(submission)
        else:
            appeal.status = "rejected"
            submission.appeal_status = "rejected"
            self.db.save_submission(submission)

        self.db.save_appeal(appeal)

        if self._notify_callback:
            try:
                if approved:
                    new_score = appeal.new_score or submission.score
                    notify_msg = (
                        f"Your appeal has been approved.\n"
                        f"New score: {new_score}\n"
                        f"Reviewer: {reviewer_id}"
                    )
                else:
                    notify_msg = (
                        f"Your appeal has been rejected.\n"
                        f"Original score remains unchanged ({submission.score})\n"
                        f"Reviewer: {reviewer_id}"
                    )
                callback_result = self._notify_callback(submission.student_id, notify_msg)
                if inspect.isawaitable(callback_result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(callback_result)
                    except RuntimeError:
                        asyncio.run(callback_result)
                logger.info("Appeal notification scheduled for student=%s", submission.student_id)
            except Exception as e:
                logger.warning("Failed to send appeal notification: %s", e)

        return appeal

    def list_pending(self):
        return self.db.list_pending_appeals()

    @staticmethod
    def _serialize_regrade_result(result: GradingResult) -> dict:
        return {
            "submission_id": result.submission_id,
            "dimension_scores": result.dimension_scores,
            "total_score": result.total_score,
            "feedback": result.feedback,
            "confidence": result.confidence,
            "confidence_label": result.confidence_label,
            "gain_points": result.gain_points,
            "deductions": result.deductions,
            "regrade_diff": result.regrade_diff,
            "graded_by": result.graded_by,
            "graded_at": result.graded_at.isoformat(),
            "appeal_count": result.appeal_count,
            "grading_context": result.grading_context,
        }
