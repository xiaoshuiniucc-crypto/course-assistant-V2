"""
M07 — AI 批改引擎
核心模块：接收作业文本，结合评分细则，输出结构化批改结果。

MVP 策略：
- 支持 OpenAI API 调用（可替换为任意兼容 API）
- 逐维度批改，输出得分点 + 扣分点 + 引用依据
- 置信度标注（high/medium/low）
- 申诉重评
"""

import json
import os
import re
from typing import Optional

from contracts.models import (
    Rubric, Dimension, GradingResult, DimensionResult, Deduction,
)


# ── LLM 客户端 ────────────────────────────────────────────

class LLMClient:
    """轻量 OpenAI 兼容客户端，MVP 用。"""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: str = "gpt-4o-mini"):
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model

    def chat(self, prompt: str, system: str = "你是一位专业的课程助教。") -> str:
        try:
            from openai import OpenAI
            client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except ImportError:
            raise RuntimeError("请先安装 openai：pip install openai")
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败：{e}")


# ── 公开入口 ────────────────────────────────────────────────

def grade(submission_text: str, rubric: Rubric, courseware_context: str,
          llm_client: LLMClient, submission_id: str = "unknown") -> GradingResult:
    """
    主批改入口。
    submission_text: 学生作业全文
    rubric: 结构化评分细则
    courseware_context: RAG 检索到的课件相关段落（MVP 用简单文本匹配）
    llm_client: LLM 客户端
    """
    dimension_results: list[DimensionResult] = []
    all_context_parts: list[str] = []

    for dim in rubric.dimensions:
        dim_result = _grade_dimension(
            submission_text, dim, courseware_context, llm_client
        )
        dimension_results.append(dim_result)
        all_context_parts.append(f"维度【{dim.name}】评分上下文已记录")

    total = sum(dr.score for dr in dimension_results)

    # 检查硬性扣分项
    for rule in rubric.hard_deductions:
        # MVP 简单处理：让 LLM 判断是否触发
        triggered = _check_hard_rule(submission_text, rule, llm_client)
        if triggered:
            total += rule.penalty  # penalty 是负数
            dimension_results.append(DimensionResult(
                name=f"硬性扣分：{rule.condition}",
                score=rule.penalty,
                max_score=0,
                gain_points=[],
                deductions=[Deduction(
                    point=f"触发硬性扣分项：{rule.condition}",
                    deduct=rule.penalty,
                    evidence=rule.condition,
                    evidence_source="评分细则-硬性扣分项",
                )],
                dimension_comment=f"触发硬性扣分项「{rule.condition}」，扣 {abs(rule.penalty)} 分",
            ))

    # 置信度判断
    confidence = _determine_confidence(dimension_results, courseware_context)

    return GradingResult(
        submission_id=submission_id,
        total_score=max(0, total),  # 不低于 0 分
        dimensions=dimension_results,
        confidence=confidence,
        grading_context="\n".join(all_context_parts),
    )


def regrade_with_appeal(submission_text: str, original_result: GradingResult,
                         rubric: Rubric, appeal_reason: str,
                         courseware_context: str, llm_client: LLMClient) -> GradingResult:
    """申诉重评：在原始批改基础上，结合申诉理由重新评分。"""
    dimension_results: list[DimensionResult] = []

    for dim in rubric.dimensions:
        # 找到原始该维度的结果
        original_dim = next(
            (d for d in original_result.dimensions if d.name == dim.name), None
        )
        dim_result = _regrade_dimension(
            submission_text, dim, courseware_context, appeal_reason,
            original_dim, llm_client
        )
        dimension_results.append(dim_result)

    total = sum(dr.score for dr in dimension_results)
    confidence = _determine_confidence(dimension_results, courseware_context)

    return GradingResult(
        submission_id=original_result.submission_id,
        total_score=max(0, total),
        dimensions=dimension_results,
        confidence=confidence,
        grading_context=f"申诉重评 | 原始上下文: {original_result.grading_context} | 申诉理由: {appeal_reason}",
    )


# ── 单维度批改 ────────────────────────────────────────────

def _grade_dimension(submission_text: str, dim: Dimension,
                     courseware: str, llm: LLMClient) -> DimensionResult:
    """对单个维度进行 AI 批改。"""
    prompt = f"""你是一位课程助教，请根据以下信息批改学生的作业。

【评分维度】{dim.name}
【满分】{dim.max_score}
【评分标准】{dim.criteria}

【课件参考资料】
{courseware[:3000] if courseware else "（无课件参考资料）"}

【学生作业内容】
{submission_text[:4000]}

请严格按以下 JSON 格式输出，不要添加任何其他内容：
{{
  "score": 分数(整数),
  "gain_points": ["得分点1", "得分点2"],
  "deductions": [
    {{"point": "扣分描述", "deduct": 扣分值(正整数), "evidence": "引用依据", "evidence_source": "课件P12 或 评分细则"}}
  ],
  "comment": "该维度总评",
  "confidence": "high 或 medium 或 low"
}}
"""
    response = llm.chat(prompt)
    return _parse_dimension_response(response, dim)


def _regrade_dimension(submission_text: str, dim: Dimension,
                        courseware: str, appeal_reason: str,
                        original_dim: Optional[DimensionResult],
                        llm: LLMClient) -> DimensionResult:
    """申诉重评单维度。"""
    original_info = ""
    if original_dim:
        original_info = f"\n【原始评分】{original_dim.score}/{original_dim.max_score}\n原始评语：{original_dim.dimension_comment}"

    prompt = f"""你是一位课程助教，学生对该维度的评分提出了申诉，请重新评估。

【评分维度】{dim.name}
【满分】{dim.max_score}
【评分标准】{dim.criteria}
{original_info}
【申诉理由】{appeal_reason}

【课件参考资料】
{courseware[:3000] if courseware else "（无课件参考资料）"}

【学生作业内容】
{submission_text[:4000]}

请严格按以下 JSON 格式输出：
{{
  "score": 分数(整数),
  "gain_points": ["得分点1", "得分点2"],
  "deductions": [
    {{"point": "扣分描述", "deduct": 扣分值(正整数), "evidence": "引用依据", "evidence_source": "来源"}}
  ],
  "comment": "重评说明：与原评分相比有哪些变化，为什么变化",
  "confidence": "high 或 medium 或 low"
}}
"""
    response = llm.chat(prompt)
    return _parse_dimension_response(response, dim)


# ── 硬性扣分项检查 ────────────────────────────────────────

def _check_hard_rule(submission_text: str, rule, llm: LLMClient) -> bool:
    """让 LLM 判断是否触发硬性扣分项。"""
    prompt = f"""请判断学生作业是否触发了以下硬性扣分条件。

【扣分条件】{rule.condition}
【学生作业节选】
{submission_text[:2000]}

只需回答"是"或"否"。"""
    response = llm.chat(prompt)
    return "是" in response and "否" not in response


# ── 置信度判断 ────────────────────────────────────────────

def _determine_confidence(results: list[DimensionResult],
                           courseware: str) -> str:
    """根据扣分证据充分性和课件参考质量判断整体置信度。"""
    if not courseware or len(courseware.strip()) < 50:
        return "low"

    low_evidence_count = 0
    for dr in results:
        for dd in dr.deductions:
            if not dd.evidence or dd.evidence_source == "评分细则" and not dd.evidence.strip():
                low_evidence_count += 1

    if low_evidence_count > 2:
        return "low"
    elif low_evidence_count > 0:
        return "medium"
    return "high"


# ── 响应解析 ──────────────────────────────────────────────

def _parse_dimension_response(response: str, dim: Dimension) -> DimensionResult:
    """解析 LLM 返回的 JSON 为 DimensionResult。"""
    try:
        # 提取 JSON
        json_match = re.search(r'\{[\s\S]+\}', response)
        if not json_match:
            raise ValueError("无法从响应中提取 JSON")
        data = json.loads(json_match.group(0))

        score = min(int(data.get("score", 0)), dim.max_score)
        gain_points = data.get("gain_points", [])
        deductions = []
        for dd in data.get("deductions", []):
            deductions.append(Deduction(
                point=dd.get("point", ""),
                deduct=int(dd.get("deduct", 0)),
                evidence=dd.get("evidence", ""),
                evidence_source=dd.get("evidence_source", ""),
            ))
        comment = data.get("comment", "")

        return DimensionResult(
            name=dim.name,
            score=score,
            max_score=dim.max_score,
            gain_points=gain_points,
            deductions=deductions,
            dimension_comment=comment,
        )
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        # 解析失败，返回零分 + 说明
        return DimensionResult(
            name=dim.name,
            score=0,
            max_score=dim.max_score,
            gain_points=[],
            deductions=[Deduction(
                point="AI 响应解析失败，需人工复核",
                deduct=dim.max_score,
                evidence=f"解析错误：{e}",
                evidence_source="系统",
            )],
            dimension_comment=f"AI 响应解析失败，原始输出：{response[:200]}",
        )
