"""
数据模型定义 — 两人协作的"合同"
所有 dataclass 集中在此，A/B 双方共享同一份定义。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ─────────────────────────────────────────
# 文件解析结果
# ─────────────────────────────────────────

@dataclass
class ParsedFile:
    text: str                     # 提取的纯文本
    pages: list[str]              # 按页分割（PDF 有效）
    code_files: dict[str, str]    # zip 内的代码文件 {filename: content}
    metadata: dict                # 文件名、大小、格式等


# ─────────────────────────────────────────
# 评分细则
# ─────────────────────────────────────────

@dataclass
class Dimension:
    name: str           # "问题分析"
    max_score: int      # 30
    criteria: str       # "是否准确识别核心问题"


@dataclass
class HardRule:
    condition: str      # "抄袭"
    penalty: int        # -100


@dataclass
class Rubric:
    assignment_id: str
    course_id: str
    title: str
    total_score: int
    deadline: Optional[datetime]
    dimensions: list[Dimension]
    hard_deductions: list[HardRule]
    version: int = 1


# ─────────────────────────────────────────
# 批改结果
# ─────────────────────────────────────────

@dataclass
class Deduction:
    point: str          # 扣分描述
    deduct: int         # 扣分值
    evidence: str       # 引用依据
    evidence_source: str # "课件P12" / "评分细则"


@dataclass
class DimensionResult:
    name: str
    score: int
    max_score: int
    gain_points: list[str]
    deductions: list[Deduction]
    dimension_comment: str


@dataclass
class GradingResult:
    submission_id: str
    total_score: int
    dimensions: list[DimensionResult]
    confidence: str            # "high" / "medium" / "low"
    grading_context: str       # AI 使用的完整上下文（用于申诉回溯）


# ─────────────────────────────────────────
# 申诉
# ─────────────────────────────────────────

@dataclass
class AppealRecord:
    id: Optional[int]
    submission_id: str
    student_qq: str
    student_reason: str
    original_score: int
    ai_new_score: Optional[int]
    regrade_detail: Optional[str]   # JSON 格式的 diff
    ta_decision: Optional[str]      # "approved" / "rejected"
    ta_note: Optional[str]
    status: str                     # APPEALING / UNDER_REVIEW / APPEAL_APPROVED / APPEAL_REJECTED
    created_at: Optional[datetime]
    decided_at: Optional[datetime]


# ─────────────────────────────────────────
# 作业提交
# ─────────────────────────────────────────

@dataclass
class Submission:
    id: str
    student_qq: str
    course_id: str
    assignment_id: str
    file_path: str
    file_type: str
    submitted_at: datetime
    is_late: bool
    status: str          # submitted / grading / graded / appealing / appeal_approved / appeal_rejected
    score: Optional[int]


# ─────────────────────────────────────────
# 意图识别
# ─────────────────────────────────────────

@dataclass
class Intent:
    action: str            # UPLOAD_COURSEWARE / SET_RUBRIC / SUBMIT_HOMEWORK / APPEAL / ...
    params: dict = field(default_factory=dict)   # 提取的参数
    confidence: float = 1.0   # 匹配置信度


@dataclass
class Session:
    user_qq: str
    state: str = "IDLE"        # IDLE / WAITING_COURSE_CONFIRM / WAITING_APPEAL_REASON / ...
    data: dict = field(default_factory=dict)   # 会话上下文数据
    updated_at: Optional[datetime] = None


# ─────────────────────────────────────────
# 知识库检索
# ─────────────────────────────────────────

@dataclass
class SearchResult:
    text: str            # 匹配的文本段落
    source: str          # "第3章-数据库设计.pptx"
    page: Optional[int] = None    # 页码
    chapter: Optional[str] = None # 章节标题
    score: float = 0.0           # 相似度分数


# ─────────────────────────────────────────
# 助教指令
# ─────────────────────────────────────────

@dataclass
class TAInstruction:
    action: str       # "approve" / "reject"
    note: str = ""    # 助教备注/驳回原因


# ─────────────────────────────────────────
# 教师统计
# ─────────────────────────────────────────

@dataclass
class ProgressInfo:
    course_id: str
    assignment_id: str
    assignment_title: str
    deadline: Optional[str]
    total_students: int
    submitted_count: int
    graded_count: int
    pending_count: int
    appealing_count: int
    low_confidence_count: int


@dataclass
class Alert:
    alert_type: str    # "low_confidence" / "plagiarism" / "low_submit_rate"
    level: str         # "info" / "warning"
    message: str
    details: dict = field(default_factory=dict)
