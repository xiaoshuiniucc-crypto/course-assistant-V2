"""
数据契约 / 领域模型
所有模块共享的数据结构定义
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any
import json


# ─────────────────────────────────────────────
# 枚举 / 常量
# ─────────────────────────────────────────────

class SubmitStatus:
    PENDING = "pending"
    SUBMITTED = "submitted"
    GRADED = "graded"
    APPEAL = "appeal"
    FINAL = "final"


class IntentType:
    UPLOAD_COURSEWARE = "upload_courseware"
    SET_RUBRIC = "set_rubric"
    SET_ASSIGNMENT = "set_assignment"
    SUBMIT_HOMEWORK = "submit_homework"
    APPEAL = "appeal"
    VIEW_REPORT = "view_report"
    TA_APPROVE = "ta_approve"
    TA_REJECT = "ta_reject"
    QUERY_PROGRESS = "query_progress"
    ASK_QUESTION = "ask_question"
    UNKNOWN = "unknown"


class MessageType:
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


# ─────────────────────────────────────────────
# 基础数据类
# ─────────────────────────────────────────────

@dataclass
class QQMessage:
    """QQ 消息封装"""
    user_id: str
    group_id: Optional[str]
    message_id: str
    content: str
    timestamp: datetime
    message_type: str = MessageType.PRIVATE
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    raw: Optional[Dict] = None


@dataclass
class Courseware:
    """课件"""
    id: str
    title: str
    content: str          # 解析后的纯文本
    file_path: str
    uploaded_by: str
    created_at: datetime
    meta: Dict = field(default_factory=dict)


@dataclass
class RubricDimension:
    """评分维度"""
    name: str
    weight: float          # 0~1
    description: str = ""
    max_score: float = 100.0


@dataclass
class Rubric:
    """评分标准"""
    id: str
    title: str
    dimensions: List[RubricDimension]
    total_score: float = 100.0
    hard_rules: List[str] = field(default_factory=list)   # e.g. "迟到扣10分"
    created_at: datetime = field(default_factory=datetime.now)

    def calc_total_weight(self) -> float:
        return sum(d.weight for d in self.dimensions)


@dataclass
class Assignment:
    """作业"""
    id: str
    title: str
    description: str
    rubric_id: Optional[str]
    courseware_id: Optional[str]
    deadline: Optional[datetime]
    max_score: float = 100.0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class HomeworkSubmission:
    """学生提交"""
    id: str
    assignment_id: str
    student_id: str
    student_name: str
    content: str          # 提交文本
    file_path: Optional[str]
    status: str = SubmitStatus.PENDING
    submitted_at: datetime = field(default_factory=datetime.now)
    score: Optional[float] = None
    feedback: Optional[str] = None
    graded_at: Optional[datetime] = None
    plagiarized: bool = False
    appeal_status: str = "none"   # none / pending / approved / rejected


@dataclass
class GradingResult:
    """批改结果"""
    submission_id: str
    dimension_scores: Dict[str, float]    # dimension_name → score
    total_score: float
    feedback: str
    confidence: float       # 0~1，AI 批改置信度
    graded_by: str = "ai"  # ai / ta / teacher
    graded_at: datetime = field(default_factory=datetime.now)
    appeal_count: int = 0


@dataclass
class Appeal:
    """申诉"""
    id: str
    submission_id: str
    student_id: str
    reason: str
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "pending"   # pending / approved / rejected
    new_score: Optional[float] = None
    reviewed_by: Optional[str] = None


@dataclass
class StudentProgress:
    """学生学习进度"""
    student_id: str
    assignment_id: str
    submitted: bool
    score: Optional[float]
    rank_percentile: Optional[float]
    anomalies: List[str] = field(default_factory=list)


@dataclass
class TeacherAlert:
    """教师预警"""
    alert_type: str      # low_confidence / plagiarism / low_submit_rate
    severity: str        # info / warning / critical
    message: str
    related_ids: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class IntentResult:
    """意图识别结果"""
    intent: str
    confidence: float
    session_id: str
    waiting_for: Optional[str] = None   # 多轮对话等待的字段
    slots: Dict[str, Any] = field(default_factory=dict)
