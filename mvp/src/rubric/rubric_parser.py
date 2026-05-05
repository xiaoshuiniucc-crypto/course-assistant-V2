"""
M04 — 评分细则解析
双层策略：
1. 结构化解析（优先）：识别 Markdown 表格、编号列表
2. LLM 语义解析（兜底）：用 prompt 将自然语言转为结构化数据

MVP 阶段先实现结构化解析 + 简单的 LLM 兜底。
"""

import json
import re
from datetime import datetime
from typing import Optional

from contracts.models import Rubric, Dimension, HardRule


# ── 公开入口 ────────────────────────────────────────────────

def parse(text: str, tag_type: str, course_id: str, assignment_id: str,
          llm_client=None) -> Rubric:
    """
    解析评分细则 / 作业要求文本。
    先尝试结构化解析，失败则走 LLM 兜底。
    """
    rubric = _try_structured_parse(text, course_id, assignment_id)
    if rubric:
        return rubric

    # 结构化解析失败 → LLM 兜底
    if llm_client:
        return _llm_fallback_parse(text, course_id, assignment_id, llm_client)

    # 都失败了，返回一个最基础的空细则
    return Rubric(
        assignment_id=assignment_id,
        course_id=course_id,
        title="未识别的作业",
        total_score=100,
        deadline=None,
        dimensions=[Dimension(name="综合评分", max_score=100, criteria="按作业完成度评分")],
        hard_deductions=[],
    )


# ── 结构化解析 ──────────────────────────────────────────────

def _try_structured_parse(text: str, course_id: str, assignment_id: str) -> Optional[Rubric]:
    """
    尝试从文本中提取：
    - 标题（## 作业名称）
    - 总分（## 总分：100分）
    - 截止时间
    - Markdown 表格格式的评分维度
    - 编号列表格式的硬性扣分项
    """
    title = _extract_title(text) or "未命名作业"
    total_score = _extract_total_score(text)
    deadline = _extract_deadline(text)
    dimensions = _extract_dimensions(text, total_score)
    hard_deductions = _extract_hard_deductions(text)

    if not dimensions:
        # 没提取到维度，说明文本格式不支持结构化解析
        return None

    # 校验维度总分
    dim_sum = sum(d.max_score for d in dimensions)
    if dim_sum != total_score and total_score == 100:
        # 维度总分 != 声明总分，用维度总和为准
        total_score = dim_sum

    return Rubric(
        assignment_id=assignment_id,
        course_id=course_id,
        title=title,
        total_score=total_score,
        deadline=deadline,
        dimensions=dimensions,
        hard_deductions=hard_deductions,
    )


# ── 标题提取 ─────────────────────────────────────────────

def _extract_title(text: str) -> Optional[str]:
    # 匹配 "## 作业名称：XXX" 或 "作业名称：XXX"
    m = re.search(r'(?:##\s*)?作业(?:名称|名)[：:]\s*(.+)', text)
    if m:
        return m.group(1).strip()
    m = re.search(r'(?:##\s*)?标题[：:]\s*(.+)', text)
    if m:
        return m.group(1).strip()
    # 兜底：取第一个 ## 标题
    m = re.search(r'##\s+(.+)', text)
    if m:
        return m.group(1).strip()
    return None


# ── 总分提取 ─────────────────────────────────────────────

def _extract_total_score(text: str) -> int:
    m = re.search(r'(?:##\s*)?总分[：:]\s*(\d+)\s*分?', text)
    if m:
        return int(m.group(1))
    return 100  # 默认100分


# ── 截止时间提取 ─────────────────────────────────────────

def _extract_deadline(text: str) -> Optional[datetime]:
    m = re.search(r'(?:##\s*)?截止(?:时间|日期)[：:]\s*(.+)', text)
    if m:
        date_str = m.group(1).strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y年%m月%d日 %H:%M"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
    return None


# ── 维度提取（Markdown 表格）────────────────────────────

def _extract_dimensions(text: str, total_score: int) -> list[Dimension]:
    dimensions: list[Dimension] = []

    # 模式1: Markdown 表格
    # | 维度 | 满分 | 评分要点 |
    # |------|------|----------|
    # | 问题分析 | 30 | 是否准确识别核心问题 |
    table_pattern = re.compile(
        r'\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|',
        re.MULTILINE
    )
    for m in table_pattern.finditer(text):
        name = m.group(1).strip()
        max_score = int(m.group(2))
        criteria = m.group(3).strip()
        # 跳过表头行和分隔行
        if name in ("维度", "项目", "评分维度"):
            continue
        if re.match(r'^[-:]+$', name) or re.match(r'^[-:]+$', criteria):
            continue
        dimensions.append(Dimension(name=name, max_score=max_score, criteria=criteria))

    if dimensions:
        return dimensions

    # 模式2: 编号列表
    # 1. 问题分析（30分）：是否准确识别核心问题
    list_pattern = re.compile(
        r'\d+[.、]\s*(.+?)[（(]\s*(\d+)\s*分?\s*[）)]\s*[：:]\s*(.+)',
        re.MULTILINE
    )
    for m in list_pattern.finditer(text):
        name = m.group(1).strip()
        max_score = int(m.group(2))
        criteria = m.group(3).strip()
        dimensions.append(Dimension(name=name, max_score=max_score, criteria=criteria))

    return dimensions


# ── 硬性扣分项提取 ───────────────────────────────────────

def _extract_hard_deductions(text: str) -> list[HardRule]:
    deductions: list[HardRule] = []
    # 匹配 "### 硬性扣分项" 后面的 "- 抄袭：直接判0分"
    in_section = False
    for line in text.split("\n"):
        if re.search(r'硬性扣分', line):
            in_section = True
            continue
        if in_section:
            # 模式1: "抄袭：直接判0分" → penalty = -100（总分归零）
            m = re.match(r'[-*]\s*(.+?)[：:]\s*直接判0分', line.strip())
            if m:
                condition = m.group(1).strip()
                deductions.append(HardRule(condition=condition, penalty=-100))
                continue
            # 模式2: "未按格式提交：扣10分" → penalty = -10
            m = re.match(r'[-*]\s*(.+?)[：:]\s*(?:扣?)(\d+)\s*分', line.strip())
            if m:
                condition = m.group(1).strip()
                penalty = -int(m.group(2))
                deductions.append(HardRule(condition=condition, penalty=penalty))
                continue
            if line.strip().startswith("#"):
                in_section = False
    return deductions


# ── LLM 兜底解析 ──────────────────────────────────────────

def _llm_fallback_parse(text: str, course_id: str, assignment_id: str,
                         llm_client) -> Rubric:
    """用 LLM 将自然语言评分细则转为结构化数据。"""
    prompt = f"""请将以下评分细则文档解析为 JSON 格式。严格按如下结构输出，不要添加任何其他内容：

{{
  "title": "作业标题",
  "total_score": 100,
  "deadline": "2026-05-10 23:59",
  "dimensions": [
    {{"name": "维度名", "max_score": 30, "criteria": "评分标准描述"}}
  ],
  "hard_deductions": [
    {{"condition": "扣分条件", "penalty": -100}}
  ]
}}

评分细则原文：
{text}
"""
    response = llm_client.chat(prompt)
    try:
        # 提取 JSON（可能被 ``` 包裹）
        json_str = response
        json_match = re.search(r'\{[\s\S]+\}', json_str)
        if json_match:
            json_str = json_match.group(0)
        data = json.loads(json_str)

        return Rubric(
            assignment_id=assignment_id,
            course_id=course_id,
            title=data.get("title", "未命名作业"),
            total_score=data.get("total_score", 100),
            deadline=datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None,
            dimensions=[Dimension(**d) for d in data.get("dimensions", [])],
            hard_deductions=[HardRule(**h) for h in data.get("hard_deductions", [])],
        )
    except (json.JSONDecodeError, KeyError) as e:
        # LLM 输出格式异常，返回空细则
        return Rubric(
            assignment_id=assignment_id,
            course_id=course_id,
            title="解析失败",
            total_score=100,
            deadline=None,
            dimensions=[Dimension(name="综合评分", max_score=100, criteria="按作业完成度评分")],
            hard_deductions=[],
        )
