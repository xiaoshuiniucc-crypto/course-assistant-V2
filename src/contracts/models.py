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
    GENERATE_STUDENT_REPORT = "generate_student_report"  # 学生个人报告（雷达图+课件依据）
    TA_APPROVE = "ta_approve"
    TA_REJECT = "ta_reject"
    TA_COMMAND = "ta_command"          # TA 工作台指令
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


# ─────────────────────────────────────────────
# M12 报告增强模型
# ─────────────────────────────────────────────

class ReportType:
    """报告类型"""
    CLASS_SUMMARY = "class_summary"           # 班级汇总报告
    STUDENT_DETAIL = "student_detail"         # 学生个人详细报告
    RADAR_COMPARISON = "radar_comparison"      # 雷达图对比报告
    COURSEWARE_EVIDENCE = "courseware_evidence"  # 课件依据报告


@dataclass
class ReportConfig:
    """报告生成配置"""
    assignment_id: str
    report_type: str = ReportType.CLASS_SUMMARY
    student_id: Optional[str] = None          # 学生个人报告时指定
    include_radar: bool = True                 # 是否包含雷达图
    include_evidence: bool = True              # 是否包含课件依据
    include_ranking: bool = True               # 是否包含排名
    include_suggestions: bool = True           # 是否包含改进建议
    comparison_students: List[str] = field(default_factory=list)  # 雷达图对比学生


@dataclass
class ReportEvidence:
    """课件依据条目"""
    dimension_name: str              # 评分维度名
    avg_score: float                 # 班级该维度平均分
    max_score: float                 # 该维度满分
    weakness_level: str              # weak / medium / strong
    courseware_refs: List[str] = field(default_factory=list)    # 引用的课件段落
    improvement_tips: List[str] = field(default_factory=list)   # 改进建议


@dataclass
class RadarData:
    """雷达图数据"""
    dimensions: List[str]                                # 维度名称列表
    class_avg: List[float]                               # 班级平均分
    student_scores: Dict[str, List[float]] = field(default_factory=dict)  # 学生ID→各维度分数
    max_scores: List[float] = field(default_factory=list)  # 各维度满分
