"""Helpers for localizing user-facing grading and report text."""
from __future__ import annotations

import re

_DIMENSION_NAME_MAP = {
    "task completion": "任务完成度",
    "assignment completion": "任务完成度",
    "content accuracy": "内容准确性",
    "accuracy": "内容准确性",
    "logic and argumentation": "逻辑与论证",
    "logic & argumentation": "逻辑与论证",
    "logic and reasoning": "逻辑与论证",
    "expression and format": "表达与规范",
    "expression and formatting": "表达与规范",
    "language and format": "表达与规范",
    "analysis depth": "分析深度",
    "case and evidence": "案例与证据",
    "code implementation and quality": "代码实现与质量",
    "code implementation and style": "代码实现与规范",
    "model training and performance": "模型训练与性能",
    "report structure and completeness": "实验报告结构与完整性",
    "experiment report structure and completeness": "实验报告结构与完整性",
    "result analysis and reflection": "结果分析与思考",
    "results analysis and reflection": "结果分析与思考",
}

_TEXT_REPLACEMENTS = [
    (
        "No readable homework content was found, so the submission could not be graded automatically.",
        "未找到可读的作业内容，系统暂时无法自动批改。",
    ),
    ("rule-based fallback", "规则回退"),
    ("Rule-based fallback without LLM references.", "规则回退，未使用大模型参考。"),
    ("Hard deduction:", "硬性扣分："),
    ("overall feedback and improvement suggestions", "总体评语与改进建议"),
    ("what the student did well", "学生做得好的地方"),
    ("what is missing or incorrect", "缺失或不正确之处"),
    ("why this changed", "调整原因"),
    ("code readability", "代码可读性"),
    ("code correctness", "代码正确性"),
    ("coding style", "编码规范"),
    ("cnn architecture design", "CNN 架构设计"),
    ("data preprocessing", "数据预处理"),
    ("data augmentation", "数据增强"),
    ("hyperparameter setting", "超参数设置"),
    ("test accuracy", "测试准确率"),
    ("training loss convergence", "训练损失收敛情况"),
    ("report structure", "报告结构"),
    ("experimental results analysis", "实验结果分析"),
    ("improvement suggestions", "改进建议"),
    ("clarity of expression", "表达清晰度"),
    ("formatting", "格式规范"),
]

_CONFIDENCE_LABEL_MAP = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

_SUBMISSION_STATUS_MAP = {
    "pending": "待处理",
    "submitted": "已提交",
    "graded": "已批改",
    "appeal": "申诉中",
    "final": "已定稿",
}

_APPEAL_STATUS_MAP = {
    "none": "",
    "pending": "待处理",
    "approved": "已批准",
    "rejected": "已驳回",
}


def _normalize_key(text: str) -> str:
    lowered = (text or "").strip().lower()
    lowered = re.sub(r"[\s_/&-]+", " ", lowered)
    lowered = re.sub(r"[^\w\s\u4e00-\u9fff]", "", lowered)
    return lowered.strip()


def localize_dimension_name(name: str) -> str:
    normalized = _normalize_key(name)
    if not normalized:
        return ""
    return _DIMENSION_NAME_MAP.get(normalized, name.strip())


def localize_user_text(text: str) -> str:
    localized = (text or "").strip()
    if not localized:
        return ""

    replaced = localized
    lowered = replaced.lower()
    for source, target in _TEXT_REPLACEMENTS:
        if source.lower() in lowered:
            replaced = re.sub(
                re.escape(source),
                target,
                replaced,
                flags=re.IGNORECASE,
            )
            lowered = replaced.lower()
    return replaced


def translate_confidence_label(label: str) -> str:
    normalized = _normalize_key(label)
    return _CONFIDENCE_LABEL_MAP.get(normalized, label or "")


def translate_submission_status(status: str) -> str:
    normalized = _normalize_key(status)
    return _SUBMISSION_STATUS_MAP.get(normalized, status or "")


def translate_appeal_status(status: str) -> str:
    normalized = _normalize_key(status)
    return _APPEAL_STATUS_MAP.get(normalized, status or "")
