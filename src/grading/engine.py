"""
AI 批改引擎
支持 DeepSeek / OpenAI API 真实批改 + 硬扣分规则
"""
from __future__ import annotations
import os
import json
import uuid
import re
import logging
from datetime import datetime
from typing import List, Optional, Dict

# 加载 .env 文件（若存在）
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path)
except ImportError:
    pass

from contracts.models import (
    GradingResult, Rubric, RubricDimension, HomeworkSubmission
)
from storage.db import DB

logger = logging.getLogger("grading")


class GradingEngine:
    """
    AI 批改引擎
    - 支持 DeepSeek / OpenAI 兼容 API 进行维度评分
    - fallback: 基于关键词的规则评分
    - 应用硬扣分规则
    - 支持申诉重批

    环境变量配置（通过 .env 文件或系统环境变量）:
      DEEPSEEK_API_KEY   — DeepSeek API Key（优先）
      DEEPSEEK_BASE_URL  — API 地址（默认 https://api.deepseek.com）
      DEEPSEEK_MODEL     — 模型名（默认 deepseek-chat）
      OPENAI_API_KEY     — OpenAI API Key（备选）
      OPENAI_BASE_URL    — OpenAI 兼容地址
      OPENAI_MODEL       — 模型名
    """

    def __init__(self, db: DB, model: str = None):
        self.db = db
        self._client = None
        self._timeout = int(os.environ.get("LLM_TIMEOUT", "60"))

        # 确定使用哪个 LLM 提供商
        self._provider, self.model, self._api_key, self._base_url = self._resolve_config(model)
        logger.info(f"批改引擎配置: provider={self._provider}, model={self.model}")

    @staticmethod
    def _resolve_config(model_override: str = None):
        """解析 LLM 配置：DeepSeek 优先，OpenAI 备选"""
        # 1. DeepSeek 配置（优先）
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
        ds_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        ds_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

        if ds_key:
            return "deepseek", (model_override or ds_model), ds_key, ds_url

        # 2. OpenAI 兼容配置（备选）
        oa_key = os.environ.get("OPENAI_API_KEY", "")
        oa_url = os.environ.get("OPENAI_BASE_URL", None)
        oa_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

        if oa_key:
            return "openai", (model_override or oa_model), oa_key, oa_url

        # 3. 无 API Key — 规则评分模式
        return "rule", "rule-engine", "", None

    @property
    def client(self):
        """延迟初始化 OpenAI 兼容客户端"""
        if self._client is None and self._api_key:
            try:
                from openai import OpenAI
                kwargs = {"api_key": self._api_key}
                if self._base_url:
                    kwargs["base_url"] = self._base_url
                self._client = OpenAI(**kwargs)
                logger.info(f"LLM 客户端已初始化: {self._provider} @ {self._base_url or 'default'}")
            except ImportError:
                logger.warning("openai 包未安装，无法使用 AI 批改")
                self._client = None
            except Exception as e:
                logger.error(f"LLM 客户端初始化失败: {e}")
                self._client = None
        return self._client

    def grade(self, submission: HomeworkSubmission,
              rubric: Rubric,
              assignment_title: str = "") -> GradingResult:
        """
        批改一份作业
        1. 调用 AI 进行维度评分
        2. 应用硬扣分规则
        3. 计算总分
        """
        if self.client:
            result = self._grade_with_ai(submission, rubric, assignment_title)
        else:
            result = self._grade_with_rules(submission, rubric)

        # 应用硬扣分规则
        result = self._apply_hard_rules(result, rubric, submission)

        # 保存结果
        self.db.save_grading_result(result)

        # 更新提交
        submission.score = result.total_score
        submission.feedback = result.feedback
        submission.graded_at = datetime.now()
        from contracts.models import SubmitStatus
        submission.status = SubmitStatus.GRADED
        self.db.save_submission(submission)

        return result

    def regrade_for_appeal(self, submission: HomeworkSubmission,
                           rubric: Rubric, appeal_reason: str,
                           assignment_title: str = "") -> GradingResult:
        """申诉重批 — 带申诉原因的二次评分"""
        if self.client:
            result = self._grade_with_ai(
                submission, rubric, assignment_title,
                appeal_reason=appeal_reason
            )
        else:
            result = self._grade_with_rules(submission, rubric)
            # 申诉重批时适当提分（模拟 TA 复查效果）
            for dim in result.dimension_scores:
                result.dimension_scores[dim] = min(
                    result.dimension_scores[dim] * 1.15, 100
                )
            result.total_score = self._calc_weighted_score(
                result.dimension_scores, rubric
            )

        result = self._apply_hard_rules(result, rubric, submission)
        result.appeal_count += 1
        result.graded_by = "appeal_regrade"

        self.db.save_grading_result(result)

        submission.score = result.total_score
        submission.feedback = result.feedback
        submission.graded_at = datetime.now()
        self.db.save_submission(submission)

        return result

    # ── AI 评分 ───────────────────────────────

    def _grade_with_ai(self, submission: HomeworkSubmission,
                       rubric: Rubric,
                       assignment_title: str,
                       appeal_reason: str = "",
                       max_retries: int = 2) -> GradingResult:
        """使用 DeepSeek / OpenAI API 进行维度评分（含重试）"""
        dim_desc = "\n".join(
            f"- {d.name} (权重 {d.weight:.0%}): {d.description or '无描述'}"
            for d in rubric.dimensions
        )

        appeal_note = ""
        if appeal_reason:
            appeal_note = (
                f"\n\n【申诉说明】学生申诉原因: {appeal_reason}\n"
                "请重新评估，如果申诉合理请适当调整分数。"
            )

        prompt = f"""你是一位专业的课程助教，请按以下评分维度批改学生作业。

## 作业题目
{assignment_title or '（未指定）'}

## 评分维度
{dim_desc}

## 学生提交内容
{submission.content[:3000]}
{appeal_note}

## 输出要求
请严格按以下 JSON 格式输出，不要输出其他内容：
{{
  "dimension_scores": {{
    "维度1": 分数(0-100),
    "维度2": 分数(0-100)
  }},
  "feedback": "总体评价和改进建议",
  "confidence": 置信度(0-1)
}}"""

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                logger.info(
                    f"调用 {self._provider} ({self.model}) 批改提交 {submission.id}"
                    f"{' (重试 ' + str(attempt) + ')' if attempt > 0 else ''}..."
                )
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是专业课程助教，严格按照评分标准批改作业。只输出JSON。"},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=1000,
                    timeout=self._timeout,
                )
                raw = resp.choices[0].message.content.strip()
                logger.debug(f"LLM 原始响应: {raw[:200]}")

                result = self._parse_ai_response(raw, submission, rubric)
                if result:
                    logger.info(
                        f"AI 批改完成: {result.total_score}分 "
                        f"(置信度 {result.confidence})"
                    )
                    return result

                # JSON 解析失败
                last_error = "LLM 响应无法解析为 JSON"

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"AI批改异常 (尝试 {attempt + 1}/{max_retries + 1}): {e}"
                )

        logger.warning(
            f"AI批改全部失败 ({max_retries + 1}次): {last_error}, "
            f"fallback到规则评分"
        )
        return self._grade_with_rules(submission, rubric)

    def _parse_ai_response(self, raw: str,
                            submission: HomeworkSubmission,
                            rubric: Rubric) -> Optional[GradingResult]:
        """解析 AI 响应为 GradingResult（健壮版）

        改进:
        1. 多种 JSON 提取策略
        2. 维度名模糊匹配（子串包含）
        3. 缺失维度填充默认分
        """
        # 提取 JSON
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return None

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            # 尝试修复常见 JSON 问题：尾逗号、中文引号
            fixed = match.group()
            fixed = fixed.replace("，", ",")
            fixed = re.sub(r',\s*}', '}', fixed)
            fixed = re.sub(r',\s*]', ']', fixed)
            try:
                data = json.loads(fixed)
            except json.JSONDecodeError:
                return None

        dim_scores_raw = data.get("dimension_scores", {})
        if not isinstance(dim_scores_raw, dict):
            return None

        # 将字符串 key 统一，尝试数值转换
        raw_scores: Dict[str, float] = {}
        for k, v in dim_scores_raw.items():
            try:
                raw_scores[str(k)] = float(v)
            except (ValueError, TypeError):
                continue

        # 维度名匹配：精确匹配 + 模糊匹配（子串包含）
        dim_scores: Dict[str, float] = {}
        for d in rubric.dimensions:
            score = None
            # 1. 精确匹配
            if d.name in raw_scores:
                score = raw_scores[d.name]
            else:
                # 2. 模糊匹配：AI 返回的 key 包含维度名，或维度名包含 key
                for raw_key, raw_val in raw_scores.items():
                    if d.name in raw_key or raw_key in d.name:
                        score = raw_val
                        break
                    # 去除空格后匹配
                    if d.name.replace(" ", "") in raw_key.replace(" ", ""):
                        score = raw_val
                        break

            dim_scores[d.name] = round(score, 1) if score is not None else 70.0

        try:
            confidence = float(data.get("confidence", 0.7))
        except (ValueError, TypeError):
            confidence = 0.7

        return GradingResult(
            submission_id=submission.id,
            dimension_scores=dim_scores,
            total_score=self._calc_weighted_score(dim_scores, rubric),
            feedback=str(data.get("feedback", "")),
            confidence=confidence,
            graded_by=f"ai:{self._provider}",
            graded_at=datetime.now(),
        )

    # ── 规则评分 fallback ─────────────────────

    def _grade_with_rules(self, submission: HomeworkSubmission,
                          rubric: Rubric) -> GradingResult:
        """基于简单规则的评分 fallback"""
        text = submission.content
        dim_scores: Dict[str, float] = {}
        feedback_parts = []

        for d in rubric.dimensions:
            score = 65.0  # 基础分
            # 内容长度加分
            if len(text) > 500:
                score += 10
            elif len(text) > 200:
                score += 5

            # 关键词命中加分
            name_lower = d.name.lower()
            if "准确" in name_lower or "内容" in name_lower:
                if any(kw in text for kw in ["正确", "准确", "分析", "结论"]):
                    score += 10
            if "逻辑" in name_lower:
                if any(kw in text for kw in ["因此", "所以", "因为", "首先", "其次"]):
                    score += 10
            if "表达" in name_lower or "语言" in name_lower:
                if len(text) > 100 and "。" in text:
                    score += 8

            score = min(score, 100)
            dim_scores[d.name] = round(score, 1)
            feedback_parts.append(f"{d.name}: {score}分")

        total = self._calc_weighted_score(dim_scores, rubric)
        feedback = "；".join(feedback_parts) + "。（规则评分，非AI）"

        return GradingResult(
            submission_id=submission.id,
            dimension_scores=dim_scores,
            total_score=round(total, 1),
            feedback=feedback,
            confidence=0.5,
            graded_by="rule",
            graded_at=datetime.now(),
        )

    # ── 硬扣分 ────────────────────────────────

    def _apply_hard_rules(self, result: GradingResult,
                          rubric: Rubric,
                          submission: HomeworkSubmission) -> GradingResult:
        """应用硬扣分规则"""
        deductions = []
        for rule in rubric.hard_rules:
            # 解析 "迟到扣10分" 格式
            m = re.search(r"扣(\d+)分", rule)
            if m:
                pts = int(m.group(1))
                result.total_score -= pts
                deductions.append(f"硬扣分: {rule} (-{pts}分)")

        if deductions:
            result.feedback += "\n" + "\n".join(deductions)

        result.total_score = max(0, round(result.total_score, 1))
        return result

    # ── 辅助 ──────────────────────────────────

    def _calc_weighted_score(self, dim_scores: Dict[str, float],
                             rubric: Rubric) -> float:
        """计算加权总分"""
        total = 0.0
        for d in rubric.dimensions:
            s = dim_scores.get(d.name, 0)
            total += s * d.weight
        return round(total, 1)

    def batch_grade(self, submissions: List[HomeworkSubmission],
                    rubric: Rubric,
                    assignment_title: str = "") -> List[GradingResult]:
        """批量批改"""
        results = []
        for s in submissions:
            r = self.grade(s, rubric, assignment_title)
            results.append(r)
        return results
