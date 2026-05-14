"""
AI grading engine.

Supports:
- DeepSeek
- OpenAI-compatible chat completions APIs
- Rule-based fallback when no LLM is configured
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv

    _env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"
    )
    load_dotenv(_env_path)
except ImportError:
    pass

from contracts.models import (
    GradingResult,
    HomeworkSubmission,
    Rubric,
    RubricDimension,
    SearchResult,
)
from runtime.localization import localize_dimension_name, localize_user_text
from storage.db import DB

logger = logging.getLogger("grading")


def _read_secret_from_file(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        logger.warning("Failed to read API key file %s: %s", path, e)
        return ""


class GradingEngine:
    """AI grading engine with multiple providers and a rules fallback."""

    def __init__(self, db: DB, model: str = None):
        self.db = db
        self._client = None
        self._timeout = int(os.environ.get("LLM_TIMEOUT", "90"))
        self._min_timeout = int(os.environ.get("LLM_MIN_TIMEOUT", "45"))
        self._max_retries = int(os.environ.get("LLM_MAX_RETRIES", "0"))
        self._fast_grading_timeout = min(
            self._timeout,
            int(os.environ.get("LLM_GRADING_FAST_TIMEOUT", "35")),
        )
        self._grading_timeout_chars_step = int(
            os.environ.get("LLM_GRADING_TIMEOUT_CHARS_STEP", "1800")
        )
        self._grading_timeout_step_seconds = int(
            os.environ.get("LLM_GRADING_TIMEOUT_STEP_SECONDS", "20")
        )
        self._grading_fast_excerpt_chars = int(
            os.environ.get("LLM_GRADING_FAST_EXCERPT_CHARS", "2200")
        )
        self._grading_full_excerpt_chars = int(
            os.environ.get("LLM_GRADING_FULL_EXCERPT_CHARS", "5000")
        )
        self._grading_full_timeout_floor = int(
            os.environ.get("LLM_GRADING_FULL_TIMEOUT", "0")
        )
        self._grading_log_dir = Path("run_logs") / "grading"
        self._grading_log_dir.mkdir(parents=True, exist_ok=True)
        self._rubric_log_dir = Path("run_logs") / "rubrics"
        self._rubric_log_dir.mkdir(parents=True, exist_ok=True)
        self._provider, self.model, self._api_key, self._base_url = self._resolve_config(
            model
        )
        self._fast_grading_model = (
            os.environ.get("LLM_GRADING_FAST_MODEL", "").strip()
            or os.environ.get("OPENAI_FAST_MODEL", "").strip()
            or os.environ.get("ZHIPUAI_FAST_MODEL", "").strip()
            or self.model
        )
        logger.info(
            "Grading engine configured: provider=%s, model=%s, timeout=%ss, min_timeout=%ss, "
            "fast_model=%s, max_retries=%s, grading_full_timeout_floor=%ss",
            self._provider,
            self.model,
            self._timeout,
            self._min_timeout,
            self._fast_grading_model,
            self._max_retries,
            self._grading_full_timeout_floor,
        )

    @staticmethod
    def _resolve_config(model_override: str = None):
        ds_key = (
            os.environ.get("DEEPSEEK_API_KEY", "")
            or _read_secret_from_file(os.environ.get("DEEPSEEK_API_KEY_FILE", ""))
        )
        ds_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        ds_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        if ds_key:
            return "deepseek", (model_override or ds_model), ds_key, ds_url

        oa_key = (
            os.environ.get("OPENAI_API_KEY", "")
            or _read_secret_from_file(os.environ.get("OPENAI_API_KEY_FILE", ""))
            or _read_secret_from_file(os.environ.get("ZHIPUAI_API_KEY_FILE", ""))
        )
        oa_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        oa_model = (
            os.environ.get("OPENAI_MODEL", "").strip()
            or os.environ.get("ZHIPUAI_MODEL", "").strip()
            or "gpt-4o-mini"
        )
        if not oa_url and os.environ.get("ZHIPUAI_API_KEY_FILE", "").strip():
            oa_url = "https://open.bigmodel.cn/api/paas/v4/"
        if oa_key:
            return "openai", (model_override or oa_model), oa_key, oa_url

        return "rule", "rule-engine", "", None

    @property
    def client(self):
        if self._client is None and self._api_key:
            try:
                from openai import OpenAI
                import httpx

                kwargs = {"api_key": self._api_key}
                if self._base_url:
                    kwargs["base_url"] = self._base_url
                kwargs["max_retries"] = self._max_retries
                # Ignore broken system proxy settings so local QQ bot grading
                # can reach the model endpoint directly.
                kwargs["http_client"] = httpx.Client(
                    trust_env=False,
                    timeout=float(self._timeout),
                )
                self._client = OpenAI(**kwargs)
                logger.info(
                    "LLM client initialized: provider=%s, endpoint=%s",
                    self._provider,
                    self._base_url or "default",
                )
            except ImportError:
                logger.warning("openai SDK not installed, falling back to rule grading")
                self._client = None
            except Exception as e:
                logger.error("LLM client init failed: %s", e)
                self._client = None
        return self._client

    def grade(
        self, submission: HomeworkSubmission, rubric: Rubric, assignment_title: str = ""
    ) -> GradingResult:
        existing_result = self.db.get_grading_result(submission.id)
        if self.client:
            result = self._grade_with_ai(
                submission,
                rubric,
                assignment_title,
                existing_result=existing_result,
            )
        else:
            result = self._grade_with_rules(
                submission,
                rubric,
                existing_result=existing_result,
            )

        result = self._apply_hard_rules(result, rubric, submission)
        self.db.save_grading_result(result)
        self._write_grading_json(submission, rubric, result, assignment_title)

        from contracts.models import SubmitStatus

        submission.score = result.total_score
        submission.feedback = result.feedback
        submission.graded_at = datetime.now()
        submission.status = SubmitStatus.GRADED
        self.db.save_submission(submission)
        return result

    def answer_question(
        self,
        question: str,
        context_results: Optional[List[SearchResult]] = None,
        course_id: str = "default",
    ) -> str:
        question = (question or "").strip()
        if not question or not self.client:
            return ""

        context_results = context_results or []
        reference_block = self._build_reference_block(context_results)
        user_prompt = self._build_question_prompt(question, reference_block, course_id)

        try:
            logger.info(
                "Calling %s model=%s for chat question course=%s",
                self._provider,
                self.model,
                course_id,
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a helpful course assistant. "
                            "Answer in Chinese by default. "
                            "When references are provided, ground the answer in them and do not fabricate. "
                            "If the references are insufficient, clearly say so and then provide the best helpful answer you can."
                        ),
                    },
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=900,
                timeout=self._timeout,
            )
            msg = response.choices[0].message
            text = (msg.content or "").strip()
            if not text:
                text = (getattr(msg, "reasoning_content", None) or "").strip()
            return text
        except Exception as e:
            logger.warning("AI question answering failed: %s", e)
            return ""

    def regrade_for_appeal(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        appeal_reason: str,
        assignment_title: str = "",
    ) -> GradingResult:
        existing_result = self.db.get_grading_result(submission.id)
        if self.client:
            result = self._grade_with_ai(
                submission,
                rubric,
                assignment_title,
                appeal_reason=appeal_reason,
                existing_result=existing_result,
            )
        else:
            result = self._grade_with_rules(
                submission,
                rubric,
                existing_result=existing_result,
            )
            for dim in result.dimension_scores:
                result.dimension_scores[dim] = min(
                    result.dimension_scores[dim] * 1.15,
                    100,
                )
            result.total_score = self._calc_weighted_score(result.dimension_scores, rubric)

        result = self._apply_hard_rules(result, rubric, submission)
        result.appeal_count = (existing_result.appeal_count if existing_result else 0) + 1
        result.graded_by = "appeal_regrade"
        self.db.save_grading_result(result)

        submission.score = result.total_score
        submission.feedback = result.feedback
        submission.graded_at = datetime.now()
        self.db.save_submission(submission)
        return result

    def generate_rubric_draft(
        self,
        assignment_title: str,
        assignment_requirement: str,
        *,
        course_id: str = "default",
        teacher_feedback: str = "",
        previous_draft: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.client:
            logger.warning(
                "AI rubric generation unavailable for title=%s, using local fallback draft",
                assignment_title,
            )
            return self._build_fallback_rubric_payload(
                assignment_title=assignment_title,
                assignment_requirement=assignment_requirement,
                teacher_feedback=teacher_feedback,
                previous_draft=previous_draft,
                reason="No LLM client is configured.",
            )

        previous_block = ""
        if previous_draft:
            previous_block = json.dumps(previous_draft, ensure_ascii=False, indent=2)

        feedback_block = teacher_feedback.strip() or "None"
        prompt = f"""为以下作业创建评分标准。返回纯JSON，不要markdown。
所有面向教师和学生展示的字段必须使用简体中文，包括 title、summary、teacher_message、dimensions.name、dimensions.description、grading_focus、hard_rules。

作业：{assignment_title}
要求：{assignment_requirement}
课程：{course_id}
{f'之前的草案：{previous_block}' if previous_block else ''}
{f'教师修改意见：{feedback_block}' if teacher_feedback.strip() else ''}

JSON格式：{{"title":"标题","summary":"简要说明","teacher_message":"给教师的提示","dimensions":[{{"name":"维度名","weight":0.25,"description":"说明","max_score":100,"grading_focus":["要点1","要点2"]}}],"hard_rules":[]}}

要求：3-5个维度，权重之和为1.0，description简短具体可评分。"""

        logger.info(
            "Calling %s model=%s for rubric draft course=%s title=%s",
            self._provider,
            self.model,
            course_id,
            assignment_title,
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是评分标准设计专家。只返回JSON，不要markdown或其他文字。"
                            "所有可读字段都必须使用简体中文。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=800,
                timeout=self._timeout,
            )
            msg = response.choices[0].message
            raw = (msg.content or "").strip()
            # GLM-4.x models may put JSON in reasoning_content
            if not raw:
                raw = (getattr(msg, "reasoning_content", None) or "").strip()
            logger.info("AI rubric raw response for title=%s: %s", assignment_title, raw[:3000])
            payload = self._extract_json_payload(raw)
            if not payload:
                logger.error(
                    "AI rubric JSON parse failed for title=%s. Raw response (first 500 chars): %s",
                    assignment_title,
                    raw[:500],
                )
                raise ValueError(
                    f"AI did not return parseable JSON for the rubric draft. "
                    f"Raw response: {raw[:200]}"
                )

            normalized = self._normalize_rubric_payload(payload, assignment_title)
            normalized["assignment_title"] = assignment_title
            normalized["assignment_requirement"] = assignment_requirement
            normalized["course_id"] = course_id
            normalized["teacher_feedback"] = teacher_feedback.strip()
            normalized["generated_at"] = datetime.now().isoformat()
            normalized["generated_by"] = f"ai:{self._provider}"
            normalized["raw_response"] = raw
            return normalized
        except Exception as e:
            logger.warning(
                "AI rubric generation failed for title=%s: %s; using local fallback draft",
                assignment_title,
                e,
            )
            return self._build_fallback_rubric_payload(
                assignment_title=assignment_title,
                assignment_requirement=assignment_requirement,
                teacher_feedback=teacher_feedback,
                previous_draft=previous_draft,
                reason=str(e),
            )

    def rubric_from_payload(
        self,
        payload: Dict[str, Any],
        *,
        course_id: str = "default",
        rubric_id: Optional[str] = None,
    ) -> Rubric:
        dimensions: List[RubricDimension] = []
        for item in payload.get("dimensions", []):
            focus = item.get("grading_focus") or []
            focus_text = ""
            if isinstance(focus, list):
                cleaned_focus = [str(entry).strip() for entry in focus if str(entry).strip()]
                if cleaned_focus:
                    focus_text = "；关注点：" + "、".join(cleaned_focus)

            description = str(item.get("description", "")).strip() + focus_text
            dimensions.append(
                RubricDimension(
                    name=str(item.get("name", "")).strip() or "评分维度",
                    weight=self._coerce_weight(item.get("weight", 0.0)),
                    description=description.strip(),
                    max_score=self._coerce_score(item.get("max_score", 100.0), default=100.0),
                )
            )

        return Rubric(
            id=rubric_id or f"rubric_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            title=str(payload.get("title", "AI生成评分标准")).strip() or "AI生成评分标准",
            dimensions=dimensions,
            course_id=course_id,
            total_score=100.0,
            hard_rules=[str(rule).strip() for rule in payload.get("hard_rules", []) if str(rule).strip()],
            created_at=datetime.now(),
        )

    def write_rubric_json(
        self,
        payload: Dict[str, Any],
        *,
        stage: str,
        course_id: str,
        assignment_title: str,
        assignment_requirement: str,
        teacher_feedback: str = "",
        rubric_id: str = "",
        assignment_id: str = "",
    ) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = re.sub(r"[^A-Za-z0-9_\-]+", "_", assignment_title)[:40] or "assignment"
        target = self._rubric_log_dir / f"{stamp}_{stage}_{safe_title}.json"
        snapshot = {
            "stage": stage,
            "course_id": course_id,
            "assignment_id": assignment_id,
            "assignment_title": assignment_title,
            "assignment_requirement": assignment_requirement,
            "teacher_feedback": teacher_feedback,
            "rubric_id": rubric_id,
            "saved_at": datetime.now().isoformat(),
            "payload": payload,
        }
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)

    def _grade_with_ai(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        assignment_title: str,
        appeal_reason: str = "",
        existing_result: Optional[GradingResult] = None,
    ) -> GradingResult:
        submission_content = submission.content or ""
        dynamic_plan = self._build_dynamic_grading_plan(submission_content)
        logger.info(
            "Dynamic grading plan for submission=%s: content_length=%s fast_timeout=%ss "
            "estimated_full_timeout=%ss full_timeout=%ss fast_excerpt=%s full_excerpt=%s "
            "fast_max_tokens=%s full_max_tokens=%s",
            submission.id,
            dynamic_plan["content_length"],
            dynamic_plan["fast_timeout"],
            dynamic_plan["estimated_full_timeout"],
            dynamic_plan["full_timeout"],
            dynamic_plan["fast_excerpt_chars"],
            dynamic_plan["full_excerpt_chars"],
            dynamic_plan["fast_max_tokens"],
            dynamic_plan["full_max_tokens"],
        )
        reference_block = self._build_submission_reference_block(
            submission,
            rubric,
            assignment_title,
        )
        appeal_note = ""
        if appeal_reason:
            appeal_note = (
                f"\n\n【申诉背景】\n学生申诉原因：{appeal_reason}\n"
                "请重新审阅作业；如果申诉成立，请调整相应分数。\n"
                "同时说明哪些维度发生了变化，以及变化原因。"
            )

        prompt = self._build_grading_prompt(
            rubric,
            assignment_title,
            reference_block,
            submission_content[: dynamic_plan["full_excerpt_chars"]],
            appeal_note,
            compact=False,
        )
        fast_prompt = self._build_grading_prompt(
            rubric,
            assignment_title,
            reference_block,
            submission_content[: dynamic_plan["fast_excerpt_chars"]],
            appeal_note,
            compact=True,
        )

        try:
            raw = self._request_grading_json(
                submission_id=submission.id,
                primary_model=self._fast_grading_model,
                primary_prompt=fast_prompt,
                primary_timeout=dynamic_plan["fast_timeout"],
                fallback_model=self.model,
                fallback_prompt=prompt,
                fallback_timeout=dynamic_plan["full_timeout"],
                fast_max_tokens=dynamic_plan["fast_max_tokens"],
                full_max_tokens=dynamic_plan["full_max_tokens"],
            )
            data = self._extract_json_payload(raw)
            if data:
                dim_scores = self._normalize_dimension_scores(
                    data.get("dimension_scores", {}),
                    rubric,
                )
                gain_points = self._normalize_gain_points(
                    data.get("gain_points", {}),
                    rubric,
                )
                deductions = self._normalize_deductions(
                    data.get("deductions", {}),
                    rubric,
                )
                regrade_diff = self._normalize_regrade_diff(
                    data.get("regrade_diff", {}),
                    existing_result,
                    dim_scores,
                    rubric,
                )
                gain_points = self._localize_gain_points(gain_points)
                deductions = self._localize_deductions(deductions)
                regrade_diff = self._localize_regrade_diff(regrade_diff)
                return GradingResult(
                    submission_id=submission.id,
                    dimension_scores=dim_scores,
                    total_score=self._calc_weighted_score(dim_scores, rubric),
                    feedback=localize_user_text(str(data.get("feedback", "")).strip()),
                    confidence=self._coerce_confidence(data.get("confidence", 0.7)),
                    confidence_label=self._coerce_confidence_label(
                        data.get("confidence_label"),
                        data.get("confidence", 0.7),
                    ),
                    grading_context=self._build_grading_context(
                        assignment_title,
                        rubric,
                        submission,
                        appeal_reason=appeal_reason,
                        reference_block=reference_block,
                    ),
                    gain_points=gain_points,
                    deductions=deductions,
                    regrade_diff=regrade_diff,
                    graded_by=f"ai:{self._provider}",
                    graded_at=datetime.now(),
                    appeal_count=existing_result.appeal_count if existing_result else 0,
                )
            fallback_scores = self._extract_scores_from_text(raw, rubric)
            fallback_diff = self._normalize_regrade_diff(
                {},
                existing_result,
                fallback_scores,
                rubric,
            )
            if fallback_scores:
                logger.info(
                    "Recovered non-JSON AI grading output for submission=%s via text parsing",
                    submission.id,
                )
                return GradingResult(
                    submission_id=submission.id,
                    dimension_scores=fallback_scores,
                    total_score=self._calc_weighted_score(fallback_scores, rubric),
                    feedback=localize_user_text(raw[:800].strip()),
                    confidence=0.55,
                    confidence_label="medium",
                    grading_context=self._build_grading_context(
                        assignment_title,
                        rubric,
                        submission,
                        appeal_reason=appeal_reason,
                        reference_block=reference_block,
                    ),
                    gain_points={dimension.name: [] for dimension in rubric.dimensions},
                    deductions={dimension.name: [] for dimension in rubric.dimensions},
                    regrade_diff=fallback_diff,
                    graded_by=f"ai:{self._provider}:text-fallback",
                    graded_at=datetime.now(),
                    appeal_count=existing_result.appeal_count if existing_result else 0,
                )
            logger.warning("LLM response was not parseable JSON: %s", raw[:100])
        except Exception as e:
            logger.warning("AI grading failed: %s, falling back to rules", e)

        return self._grade_with_rules(submission, rubric, existing_result=existing_result)

    def _build_grading_prompt(
        self,
        rubric: Rubric,
        assignment_title: str,
        reference_block: str,
        submission_excerpt: str,
        appeal_note: str,
        *,
        compact: bool,
    ) -> str:
        dim_desc = "\n".join(
            f"- {d.name}（权重 {d.weight:.0%}）：{d.description or '无说明'}"
            for d in rubric.dimensions
        )
        hard_rule_block = ""
        if rubric.hard_rules:
            hard_rule_lines = "\n".join(f"- {rule}" for rule in rubric.hard_rules)
            hard_rule_block = (
                "\n硬性规则：\n"
                f"{hard_rule_lines}\n"
                "如果命中硬性规则，请直接在对应维度的 `dimension_scores` 和 `deductions` 中体现。"
                "不要在总分上额外重复扣减。\n"
            )
        rubric_name_example = rubric.dimensions[0].name if rubric.dimensions else "维度1"
        compact_note = (
            "请优先输出紧凑、可解析的 JSON；每个维度最多保留 3 条加分点和 3 条扣分点。"
            if compact
            else "请尽量给出具体证据和扣分说明。"
        )
        return f"""你是一名课程助教，请按照评分标准逐维度批改学生作业。
## 作业
{assignment_title or '（未提供）'}

## 评分标准
{dim_desc}

评分规则：
1. 每个维度的原始分满分都是 100 分，不是按权重折算后的分数。
2. 发现问题后按项扣分，最终维度分 = 100 - 总扣分。
3. 维度权重只用于系统最终汇总总分，你返回的 `dimension_scores` 必须始终是 0 到 100 之间的原始分。
4. 不要返回 40、30、20、10 这类“按权重折算后的维度实得分”。例如权重 40% 的维度拿到 95 分时，应返回 95，而不是 38。
5. 除维度键名外，所有文字字段必须使用简体中文。
{hard_rule_block}
{compact_note}

## 证据与上下文
{reference_block}

## 学生作业
{submission_excerpt}
{appeal_note}

## 输出要求
只返回 JSON，不要输出 markdown 或额外说明。维度键名必须严格使用评分标准中的原始名称，例如“{rubric_name_example}”：
{{
  "dimension_scores": {{
    {self._format_dim_keys_example(rubric, '0')}
  }},
  "gain_points": {{
    {self._format_dim_keys_example(rubric, '["学生做得好的地方"]')}
  }},
  "deductions": {{
    {self._format_dim_keys_example(rubric, '[{{"point": "缺失或不正确之处", "deduct": 0, "evidence": "依据", "evidence_source": "来源"}}]')}
  }},
  "feedback": "总体评语与改进建议",
  "confidence": 0.0,
  "confidence_label": "high",
  "regrade_diff": {{
    {self._format_dim_keys_example(rubric, '{{"old": 0, "new": 0, "reason": "调整原因"}}')}
  }}
}}"""

    def _build_dynamic_grading_plan(self, submission_content: str) -> Dict[str, int]:
        content_length = len((submission_content or "").strip())
        growth_steps = max(
            0,
            (content_length - self._grading_fast_excerpt_chars + self._grading_timeout_chars_step - 1)
            // max(1, self._grading_timeout_chars_step),
        )
        estimated_full_timeout = min(
            self._timeout,
            max(
                self._min_timeout,
                self._fast_grading_timeout
                + self._grading_timeout_step_seconds
                + growth_steps * self._grading_timeout_step_seconds,
            ),
        )
        full_timeout = estimated_full_timeout
        if self._grading_full_timeout_floor > 0:
            full_timeout = min(
                self._timeout,
                max(estimated_full_timeout, self._grading_full_timeout_floor),
            )
        fast_timeout = min(
            full_timeout,
            max(
                min(self._min_timeout, self._fast_grading_timeout),
                self._fast_grading_timeout + growth_steps * (self._grading_timeout_step_seconds // 2),
            ),
        )
        fast_excerpt_chars = min(
            max(self._grading_fast_excerpt_chars, 1600),
            max(self._grading_fast_excerpt_chars, content_length),
        )
        full_excerpt_chars = min(
            max(self._grading_full_excerpt_chars, fast_excerpt_chars),
            max(self._grading_full_excerpt_chars, content_length),
        )
        fast_max_tokens = min(1200, 850 + growth_steps * 80)
        full_max_tokens = min(1600, 1000 + growth_steps * 120)
        return {
            "content_length": content_length,
            "estimated_full_timeout": int(estimated_full_timeout),
            "fast_timeout": int(fast_timeout),
            "full_timeout": int(full_timeout),
            "fast_excerpt_chars": int(fast_excerpt_chars),
            "full_excerpt_chars": int(full_excerpt_chars),
            "fast_max_tokens": int(fast_max_tokens),
            "full_max_tokens": int(full_max_tokens),
        }

    def _request_grading_json(
        self,
        *,
        submission_id: str,
        primary_model: str,
        primary_prompt: str,
        primary_timeout: int,
        fallback_model: str,
        fallback_prompt: str,
        fallback_timeout: int,
        fast_max_tokens: int,
        full_max_tokens: int,
    ) -> str:
        attempts = [("fast", primary_model, primary_prompt, primary_timeout)]
        if fallback_model != primary_model or fallback_prompt != primary_prompt:
            attempts.append(("full", fallback_model, fallback_prompt, fallback_timeout))

        last_error = None
        for stage, model_name, prompt_text, timeout_seconds in attempts:
            try:
                logger.info(
                    "Calling %s model=%s for submission=%s stage=%s timeout=%ss",
                    self._provider,
                    model_name,
                    submission_id,
                    stage,
                    timeout_seconds,
                )
                response = self.client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是一名课程助教。"
                                "只返回 JSON，不要输出 markdown 或额外说明。"
                                "除维度键名外，所有文字字段必须使用简体中文。"
                                "请优先给出准确、简洁、可解析的结果。"
                            ),
                        },
                        {"role": "user", "content": prompt_text},
                    ],
                    temperature=0.1 if stage == "fast" else 0.2,
                    max_tokens=fast_max_tokens if stage == "fast" else full_max_tokens,
                    timeout=timeout_seconds,
                )
                msg = response.choices[0].message
                raw = (msg.content or "").strip()
                if not raw:
                    raw = (getattr(msg, "reasoning_content", None) or "").strip()
                logger.info(
                    "AI grading raw response for submission=%s stage=%s: %s",
                    submission_id,
                    stage,
                    raw[:2000],
                )
                logger.debug("LLM raw response: %s", raw[:200])
                if raw:
                    return raw
            except Exception as e:
                last_error = e
                logger.warning(
                    "AI grading stage=%s failed for submission=%s: %s",
                    stage,
                    submission_id,
                    e,
                )

        if last_error:
            raise last_error
        raise RuntimeError("AI grading returned empty response.")

    def _write_grading_json(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        result: GradingResult,
        assignment_title: str,
    ):
        payload = {
            "submission_id": submission.id,
            "assignment_id": submission.assignment_id,
            "assignment_title": assignment_title,
            "course_id": getattr(submission, "course_id", "default") or "default",
            "student_id": submission.student_id,
            "student_name": submission.student_name,
            "graded_by": result.graded_by,
            "total_score": result.total_score,
            "dimension_scores": result.dimension_scores,
            "feedback": result.feedback,
            "confidence": result.confidence,
            "confidence_label": result.confidence_label,
            "gain_points": result.gain_points,
            "deductions": result.deductions,
            "regrade_diff": result.regrade_diff,
            "grading_context": result.grading_context,
            "rubric": [
                {
                    "name": dimension.name,
                    "weight": dimension.weight,
                    "description": dimension.description,
                }
                for dimension in rubric.dimensions
            ],
            "file_path": submission.file_path,
            "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
            "graded_at": result.graded_at.isoformat() if result.graded_at else None,
        }
        target = self._grading_log_dir / f"{submission.id}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _normalize_rubric_payload(
        self,
        payload: Dict[str, Any],
        fallback_title: str,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Rubric payload must be a JSON object.")

        raw_dimensions = payload.get("dimensions")
        if not isinstance(raw_dimensions, list) or not raw_dimensions:
            raise ValueError("Rubric payload does not contain usable dimensions.")

        normalized_dimensions: List[Dict[str, Any]] = []
        for item in raw_dimensions:
            if not isinstance(item, dict):
                continue
            name = localize_dimension_name(str(item.get("name", "")).strip())
            if not name:
                continue
            focus = item.get("grading_focus") or []
            if not isinstance(focus, list):
                focus = [str(focus)]
            normalized_dimensions.append(
                {
                    "name": name,
                    "weight": self._coerce_weight(item.get("weight", 0.0)),
                    "description": localize_user_text(str(item.get("description", "")).strip()),
                    "max_score": self._coerce_score(item.get("max_score", 100.0), default=100.0),
                    "grading_focus": [
                        localize_user_text(str(entry).strip())
                        for entry in focus
                        if str(entry).strip()
                    ],
                }
            )

        if len(normalized_dimensions) < 3:
            raise ValueError("Rubric draft has fewer than 3 usable dimensions.")

        total_weight = sum(item["weight"] for item in normalized_dimensions)
        if total_weight <= 0:
            even_weight = round(1.0 / len(normalized_dimensions), 4)
            for item in normalized_dimensions:
                item["weight"] = even_weight
        else:
            running = 0.0
            for index, item in enumerate(normalized_dimensions):
                if index == len(normalized_dimensions) - 1:
                    item["weight"] = round(max(0.0, 1.0 - running), 4)
                else:
                    normalized = round(item["weight"] / total_weight, 4)
                    item["weight"] = normalized
                    running += normalized

        return {
            "title": localize_user_text(
                str(payload.get("title", "")).strip() or f"{fallback_title}评分标准"
            ),
            "summary": localize_user_text(str(payload.get("summary", "")).strip()),
            "teacher_message": localize_user_text(str(payload.get("teacher_message", "")).strip()),
            "dimensions": normalized_dimensions,
            "hard_rules": [
                localize_user_text(str(rule).strip())
                for rule in payload.get("hard_rules", [])
                if str(rule).strip()
            ],
        }

    def _build_fallback_rubric_payload(
        self,
        *,
        assignment_title: str,
        assignment_requirement: str,
        teacher_feedback: str = "",
        previous_draft: Optional[Dict[str, Any]] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        text = f"{assignment_title}\n{assignment_requirement}\n{teacher_feedback}".lower()

        dimensions: List[Dict[str, Any]] = [
            {
                "name": "任务完成度",
                "weight": 0.25,
                "description": "是否完整覆盖作业要求，是否回应了题目中的核心任务。",
                "max_score": 100.0,
                "grading_focus": ["是否遗漏关键任务", "是否偏题", "是否覆盖主要要求"],
            },
            {
                "name": "内容准确性",
                "weight": 0.25,
                "description": "概念、事实、方法或结论是否准确，是否存在明显错误。",
                "max_score": 100.0,
                "grading_focus": ["术语使用是否准确", "核心观点是否正确", "事实是否可靠"],
            },
            {
                "name": "逻辑与论证",
                "weight": 0.2,
                "description": "论证结构是否清晰，推理是否连贯，观点之间是否有合理支撑。",
                "max_score": 100.0,
                "grading_focus": ["结构是否清晰", "推理是否连贯", "结论是否有依据"],
            },
            {
                "name": "表达与规范",
                "weight": 0.15,
                "description": "语言表达是否清楚，格式是否规范，整体可读性是否达标。",
                "max_score": 100.0,
                "grading_focus": ["语言是否清晰", "格式是否规范", "可读性是否良好"],
            },
        ]

        if any(keyword in text for keyword in ("案例", "example", "举例", "材料")):
            dimensions.insert(
                3,
                {
                    "name": "案例与证据",
                    "weight": 0.15,
                    "description": "是否提供了恰当的案例、材料或证据来支撑观点。",
                    "max_score": 100.0,
                    "grading_focus": ["案例是否相关", "证据是否充分", "是否能支撑结论"],
                },
            )
        elif any(keyword in text for keyword in ("分析", "compare", "比较", "论证", "讨论")):
            dimensions.insert(
                3,
                {
                    "name": "分析深度",
                    "weight": 0.15,
                    "description": "是否进行了有层次的分析，而不是停留在表面描述。",
                    "max_score": 100.0,
                    "grading_focus": ["分析是否深入", "是否体现比较/论证", "是否有独立思考"],
                },
            )
        else:
            dimensions[0]["weight"] = 0.3
            dimensions[1]["weight"] = 0.3

        if previous_draft and teacher_feedback:
            for dimension in previous_draft.get("dimensions", []):
                name = str(dimension.get("name", "")).strip()
                if name and any(name in teacher_feedback for name in (name, name.lower())):
                    for current in dimensions:
                        if current["name"] == name:
                            current["description"] = str(dimension.get("description", current["description"]))

        total_weight = sum(item["weight"] for item in dimensions)
        running = 0.0
        for index, item in enumerate(dimensions):
            if index == len(dimensions) - 1:
                item["weight"] = round(max(0.0, 1.0 - running), 4)
            else:
                item["weight"] = round(item["weight"] / total_weight, 4)
                running += item["weight"]

        summary = "本地规则草案已生成，可先确认后直接投入使用。"
        if reason:
            summary = f"AI 当前不可用，已按作业要求生成本地规则草案。原因：{reason}"

        return {
            "title": f"{assignment_title}评分标准",
            "summary": summary,
            "teacher_message": "如结构基本合适，可直接确认；如需调整权重或维度，请回复修改意见。",
            "dimensions": dimensions,
            "hard_rules": [],
        }

    def _build_reference_block(self, context_results: List[SearchResult]) -> str:
        if not context_results:
            return ""

        lines = []
        for index, item in enumerate(context_results, 1):
            lines.append(
                f"[参考{index}] 来源={item.title or item.courseware_id} "
                f"片段={item.chunk_index} 相关度={item.score:.2f}\n{item.text[:500]}"
            )
        return "\n\n".join(lines)

    def _build_question_prompt(
        self,
        question: str,
        reference_block: str,
        course_id: str,
    ) -> str:
        if reference_block:
            return f"""请回答下面这个课程相关问题。

课程ID：{course_id}

用户问题：
{question}

可用参考资料：
{reference_block}

回答要求：
1. 优先基于参考资料回答。
2. 如果参考资料不足，请明确指出“根据当前已入库课件”无法完全确认，再给出尽量有帮助的解释。
3. 回答自然、简洁、适合聊天场景。
4. 如果引用了参考资料，可在结尾用“参考：xxx”简要标注来源标题。
"""

        return f"""请回答下面这个用户消息。

课程ID：{course_id}

用户消息：
{question}

回答要求：
1. 这是聊天场景，语气自然、直接。
2. 如果这是课程相关问题就尽量解答；如果是寒暄也正常回应。
3. 不要编造你已经看过某份课件，除非题目里明确给了资料。
"""

    def _grade_with_rules(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        existing_result: Optional[GradingResult] = None,
    ) -> GradingResult:
        text = (submission.content or "").strip()
        if not self._has_readable_submission_text(text):
            dim_scores = {dimension.name: 0.0 for dimension in rubric.dimensions}
            return GradingResult(
                submission_id=submission.id,
                dimension_scores=dim_scores,
                total_score=0.0,
                feedback="未找到可读的作业内容，系统暂时无法自动批改。",
                confidence=0.1,
                confidence_label="low",
                grading_context="",
                gain_points={dimension.name: [] for dimension in rubric.dimensions},
                deductions={dimension.name: [] for dimension in rubric.dimensions},
                regrade_diff={},
                graded_by="rule",
                graded_at=datetime.now(),
                appeal_count=existing_result.appeal_count if existing_result else 0,
            )

        dim_scores: Dict[str, float] = {}
        feedback_parts = []
        cleaned_text = re.sub(r"\s+", "", text)
        text_length = len(cleaned_text)
        line_count = len([line for line in text.splitlines() if line.strip()])
        sentence_hits = len(re.findall(r"[。！？.!?；;]", text))
        content_keywords = (
            "定义",
            "概念",
            "理论",
            "模型",
            "方法",
            "结论",
            "definition",
            "concept",
            "theory",
            "model",
            "method",
            "conclusion",
        )
        logic_keywords = (
            "首先",
            "其次",
            "然后",
            "最后",
            "因为",
            "因此",
            "综上",
            "first",
            "second",
            "then",
            "because",
            "therefore",
            "finally",
            "in conclusion",
        )

        for d in rubric.dimensions:
            score = 35.0
            if text_length >= 1200:
                score += 28
            elif text_length >= 600:
                score += 22
            elif text_length >= 250:
                score += 15
            elif text_length >= 80:
                score += 8
            elif text_length >= 20:
                score += 3

            if line_count >= 8:
                score += 10
            elif line_count >= 4:
                score += 6
            elif line_count >= 2:
                score += 3

            if sentence_hits >= 8:
                score += 6
            elif sentence_hits >= 4:
                score += 3

            name_lower = d.name.lower()
            if any(keyword in name_lower for keyword in ("content", "accuracy", "内容", "准确")):
                if any(kw in text for kw in content_keywords):
                    score += 12
            if any(keyword in name_lower for keyword in ("logic", "论证", "逻辑")):
                if any(kw in text for kw in logic_keywords):
                    score += 12
            if any(
                keyword in name_lower
                for keyword in ("expression", "language", "表达", "规范", "语言")
            ):
                if text_length >= 100:
                    score += 10

            score = min(score, 100)
            dim_scores[d.name] = round(score, 1)
            feedback_parts.append(f"{d.name}：{score:.1f}分")

        total = self._calc_weighted_score(dim_scores, rubric)
        feedback = "；".join(feedback_parts) + "（规则回退）"

        return GradingResult(
            submission_id=submission.id,
            dimension_scores=dim_scores,
            total_score=round(total, 1),
            feedback=feedback,
            confidence=0.5,
            confidence_label="medium",
            grading_context=self._build_grading_context(
                "",
                rubric,
                submission,
                appeal_reason="",
                reference_block="规则回退，未使用大模型参考。",
            ),
            gain_points={dimension.name: [] for dimension in rubric.dimensions},
            deductions={dimension.name: [] for dimension in rubric.dimensions},
            regrade_diff={},
            graded_by="rule",
            graded_at=datetime.now(),
            appeal_count=existing_result.appeal_count if existing_result else 0,
        )

    def _has_readable_submission_text(self, text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False

        unreadable_markers = (
            "[file submission]",
            "[PDF parser unavailable",
            "[DOCX parser unavailable",
            "[Unable to decode file:",
            "[PDF解析不可用",
            "[DOCX解析不可用",
            "[无法解码文件:",
        )
        if any(normalized.startswith(marker) for marker in unreadable_markers):
            return False

        return True

    def _apply_hard_rules(
        self,
        result: GradingResult,
        rubric: Rubric,
        submission: HomeworkSubmission,
    ) -> GradingResult:
        # Hard rules are advisory rubric constraints. They must be incorporated
        # during dimension scoring, not blindly subtracted from the final score.
        # The previous implementation deducted any number mentioned in the rule
        # text unconditionally, which could wrongly reduce the total score even
        # when the rule was not triggered.
        result.total_score = max(0, round(self._calc_weighted_score(result.dimension_scores, rubric), 1))
        return result

    def _calc_weighted_score(self, dim_scores: Dict[str, float], rubric: Rubric) -> float:
        total = 0.0
        for d in rubric.dimensions:
            total += dim_scores.get(d.name, 0) * d.weight
        return round(total, 1)

    def _looks_like_weighted_dimension_scores(
        self,
        dim_scores: Dict[str, float],
        rubric: Rubric,
    ) -> bool:
        if not dim_scores or not rubric.dimensions:
            return False

        present_scores = [dim_scores.get(d.name) for d in rubric.dimensions if d.name in dim_scores]
        if len(present_scores) != len(rubric.dimensions):
            return False

        weighted_caps = [round(d.weight * 100, 4) for d in rubric.dimensions]
        if not any(cap < 99.9 for cap in weighted_caps):
            return False

        total_score = sum(float(score) for score in present_scores)
        if not (70.0 <= total_score <= 110.0):
            return False

        within_weighted_cap = 0
        for dimension in rubric.dimensions:
            score = float(dim_scores.get(dimension.name, 0.0))
            weighted_cap = dimension.weight * 100
            if score <= weighted_cap + 3.0:
                within_weighted_cap += 1

        return within_weighted_cap >= max(2, len(rubric.dimensions) - 1)

    def _expand_weighted_dimension_scores(
        self,
        dim_scores: Dict[str, float],
        rubric: Rubric,
    ) -> Dict[str, float]:
        expanded: Dict[str, float] = {}
        for dimension in rubric.dimensions:
            weighted_score = float(dim_scores.get(dimension.name, 0.0))
            if dimension.weight <= 0:
                expanded[dimension.name] = round(weighted_score, 1)
                continue
            expanded_score = weighted_score / dimension.weight
            expanded[dimension.name] = round(min(100.0, max(0.0, expanded_score)), 1)
        return expanded

    def _extract_json_payload(self, raw: str) -> Optional[Dict[str, Any]]:
        cleaned = raw.strip()
        fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
        if fenced_match:
            cleaned = fenced_match.group(1).strip()

        # 1) Try direct parse
        for candidate in [cleaned]:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # 2) Use raw_decode to find the first valid JSON object,
        #    skipping any leading text/thinking content from the model.
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(cleaned):
            brace_pos = cleaned.find("{", idx)
            if brace_pos == -1:
                break
            try:
                parsed, _ = decoder.raw_decode(cleaned, brace_pos)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            idx = brace_pos + 1

        # 3) Greedy fallback
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        return None

    def _extract_scores_from_text(self, raw: str, rubric: Rubric) -> Dict[str, float]:
        if not raw.strip():
            return {}

        recovered: Dict[str, float] = {}
        for dimension in rubric.dimensions:
            pattern = (
                rf"{re.escape(dimension.name)}[^0-9]{{0,20}}"
                rf"([0-9]{{1,3}}(?:\.[0-9]+)?)"
            )
            match = re.search(pattern, raw, re.IGNORECASE)
            if match:
                recovered[dimension.name] = self._coerce_score(match.group(1), default=70.0)

        if recovered:
            for dimension in rubric.dimensions:
                recovered.setdefault(dimension.name, 70.0)
        return recovered

    def _normalize_dimension_scores(self, raw_scores: Any, rubric: Rubric) -> Dict[str, float]:
        normalized_scores: Dict[str, float] = {}
        if isinstance(raw_scores, dict):
            name_mapping = self._build_dimension_name_mapping(raw_scores, rubric)
            for raw_name, raw_value in raw_scores.items():
                matched_name = name_mapping.get(str(raw_name)) or self._match_dimension_name(
                    str(raw_name), rubric
                )
                score = self._coerce_score(raw_value, default=70.0)
                if matched_name:
                    normalized_scores[matched_name] = score

        for dimension in rubric.dimensions:
            normalized_scores.setdefault(dimension.name, 70.0)

        if self._looks_like_weighted_dimension_scores(normalized_scores, rubric):
            logger.warning(
                "AI returned weighted dimension scores instead of 100-point raw scores; "
                "auto-expanding before total-score calculation."
            )
            normalized_scores = self._expand_weighted_dimension_scores(normalized_scores, rubric)

        return normalized_scores

    def _build_dimension_name_mapping(
        self,
        raw_mapping: Any,
        rubric: Rubric,
    ) -> Dict[str, str]:
        if not isinstance(raw_mapping, dict):
            return {}

        mapping: Dict[str, str] = {}
        matched_targets = set()
        raw_items = list(raw_mapping.items())

        for raw_name, _ in raw_items:
            matched_name = self._match_dimension_name(str(raw_name), rubric)
            if matched_name and matched_name not in matched_targets:
                mapping[str(raw_name)] = matched_name
                matched_targets.add(matched_name)

        remaining_raw_names = [str(raw_name) for raw_name, _ in raw_items if str(raw_name) not in mapping]
        remaining_dimensions = [
            dimension.name for dimension in rubric.dimensions if dimension.name not in matched_targets
        ]

        # If the model rewrote dimension names but preserved order, align the
        # remaining keys to the remaining rubric dimensions by position.
        if remaining_raw_names and len(remaining_raw_names) == len(remaining_dimensions):
            for raw_name, dimension_name in zip(remaining_raw_names, remaining_dimensions):
                mapping[raw_name] = dimension_name

        return mapping

    def _match_dimension_name(self, raw_name: str, rubric: Rubric) -> Optional[str]:
        normalized_raw = self._normalize_dimension_name(raw_name)
        if not normalized_raw:
            return None

        for dimension in rubric.dimensions:
            if dimension.name == raw_name:
                return dimension.name

        for dimension in rubric.dimensions:
            normalized_dimension = self._normalize_dimension_name(dimension.name)
            if normalized_dimension == normalized_raw:
                return dimension.name

        for dimension in rubric.dimensions:
            normalized_dimension = self._normalize_dimension_name(dimension.name)
            if normalized_raw in normalized_dimension or normalized_dimension in normalized_raw:
                return dimension.name

        return None

    def _normalize_dimension_name(self, name: str) -> str:
        return re.sub(r"[\W_]+", "", name).lower()

    def _coerce_score(self, value: Any, default: float = 70.0) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return default
        return round(min(max(score, 0.0), 100.0), 1)

    def _coerce_confidence(self, value: Any, default: float = 0.7) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return default
        return round(min(max(confidence, 0.0), 1.0), 3)

    def _coerce_confidence_label(self, value: Any, confidence: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"high", "medium", "low"}:
            return normalized

        score = self._coerce_confidence(confidence, default=0.7)
        if score >= 0.8:
            return "high"
        if score >= 0.5:
            return "medium"
        return "low"

    def _localize_gain_points(self, gain_points: Dict[str, List[str]]) -> Dict[str, List[str]]:
        localized: Dict[str, List[str]] = {}
        for dim_name, items in (gain_points or {}).items():
            localized[dim_name] = [
                localize_user_text(str(item).strip())
                for item in items
                if str(item).strip()
            ]
        return localized

    def _localize_deductions(
        self,
        deductions: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        localized: Dict[str, List[Dict[str, Any]]] = {}
        for dim_name, items in (deductions or {}).items():
            localized_items: List[Dict[str, Any]] = []
            for item in items:
                localized_items.append(
                    {
                        **item,
                        "point": localize_user_text(str(item.get("point", "")).strip()),
                        "evidence": localize_user_text(str(item.get("evidence", "")).strip()),
                        "evidence_source": localize_user_text(
                            str(item.get("evidence_source", "")).strip()
                        ),
                    }
                )
            localized[dim_name] = localized_items
        return localized

    def _localize_regrade_diff(
        self,
        diff: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        localized: Dict[str, Dict[str, Any]] = {}
        for dim_name, item in (diff or {}).items():
            if not isinstance(item, dict):
                continue
            localized[dim_name] = {
                **item,
                "reason": localize_user_text(str(item.get("reason", "")).strip()),
            }
        return localized

    def _coerce_weight(self, value: Any, default: float = 0.0) -> float:
        try:
            weight = float(value)
        except (TypeError, ValueError):
            return default
        if weight > 1.0:
            weight /= 100.0
        return round(min(max(weight, 0.0), 1.0), 4)

    @staticmethod
    def _format_dim_keys_example(rubric: "Rubric", example_value: str) -> str:
        """Generate JSON keys using actual rubric dimension names for the prompt template."""
        return ",\n    ".join(f'"{d.name}": {example_value}' for d in rubric.dimensions)

    def _normalize_gain_points(self, raw_gain_points: Any, rubric: Rubric) -> Dict[str, List[str]]:
        normalized: Dict[str, List[str]] = {}
        if isinstance(raw_gain_points, dict):
            name_mapping = self._build_dimension_name_mapping(raw_gain_points, rubric)
            for raw_name, raw_items in raw_gain_points.items():
                matched_name = name_mapping.get(str(raw_name)) or self._match_dimension_name(
                    str(raw_name), rubric
                )
                if not matched_name:
                    continue
                items = raw_items if isinstance(raw_items, list) else [raw_items]
                normalized[matched_name] = [
                    str(item).strip() for item in items if str(item).strip()
                ]

        for dimension in rubric.dimensions:
            normalized.setdefault(dimension.name, [])
        return normalized

    def _normalize_deductions(self, raw_deductions: Any, rubric: Rubric) -> Dict[str, List[Dict[str, Any]]]:
        normalized: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(raw_deductions, dict):
            name_mapping = self._build_dimension_name_mapping(raw_deductions, rubric)
            for raw_name, raw_items in raw_deductions.items():
                matched_name = name_mapping.get(str(raw_name)) or self._match_dimension_name(
                    str(raw_name), rubric
                )
                if not matched_name:
                    continue
                items = raw_items if isinstance(raw_items, list) else [raw_items]
                cleaned_items: List[Dict[str, Any]] = []
                for item in items:
                    if isinstance(item, dict):
                        cleaned_items.append(
                            {
                                "point": str(item.get("point", "")).strip(),
                                "deduct": self._coerce_score(item.get("deduct", 0.0), default=0.0),
                                "evidence": str(item.get("evidence", "")).strip(),
                                "evidence_source": str(item.get("evidence_source", "")).strip(),
                            }
                        )
                    elif str(item).strip():
                        cleaned_items.append(
                            {
                                "point": str(item).strip(),
                                "deduct": 0.0,
                                "evidence": "",
                                "evidence_source": "",
                            }
                        )
                normalized[matched_name] = cleaned_items

        for dimension in rubric.dimensions:
            normalized.setdefault(dimension.name, [])
        return normalized

    def _normalize_regrade_diff(
        self,
        raw_diff: Any,
        existing_result: Optional[GradingResult],
        new_scores: Dict[str, float],
        rubric: Rubric,
    ) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        previous_scores = existing_result.dimension_scores if existing_result else {}

        if isinstance(raw_diff, dict):
            name_mapping = self._build_dimension_name_mapping(raw_diff, rubric)
            for raw_name, raw_item in raw_diff.items():
                matched_name = name_mapping.get(str(raw_name)) or self._match_dimension_name(
                    str(raw_name), rubric
                )
                if not matched_name or not isinstance(raw_item, dict):
                    continue
                normalized[matched_name] = {
                    "old": self._coerce_score(raw_item.get("old", previous_scores.get(matched_name, 0.0)), default=0.0),
                    "new": self._coerce_score(raw_item.get("new", new_scores.get(matched_name, 0.0)), default=0.0),
                    "reason": str(raw_item.get("reason", "")).strip(),
                }

        if existing_result:
            for dimension in rubric.dimensions:
                old_score = previous_scores.get(dimension.name, 0.0)
                new_score = new_scores.get(dimension.name, 0.0)
                if dimension.name not in normalized and round(old_score, 1) != round(new_score, 1):
                    normalized[dimension.name] = {
                        "old": old_score,
                        "new": new_score,
                        "reason": "Score changed during appeal regrade.",
                    }
        return normalized

    def _build_grading_context(
        self,
        assignment_title: str,
        rubric: Rubric,
        submission: HomeworkSubmission,
        *,
        appeal_reason: str,
        reference_block: str,
    ) -> str:
        rubric_lines = [
            f"{dimension.name} ({dimension.weight:.0%}): {dimension.description}"
            for dimension in rubric.dimensions
        ]
        parts = [
            f"Assignment: {assignment_title or '(not specified)'}",
            "Rubric:",
            "\n".join(rubric_lines),
            "Student Submission:",
            submission.content[:3000],
        ]
        if reference_block:
            parts.extend(["Evidence and Context:", reference_block[:2000]])
        if appeal_reason:
            parts.extend(["Appeal Reason:", appeal_reason[:1000]])
        return "\n\n".join(part for part in parts if part)

    def _build_submission_reference_block(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        assignment_title: str,
    ) -> str:
        lines = [
            f"Course ID: {getattr(submission, 'course_id', 'default') or 'default'}",
            f"Assignment Title: {assignment_title or '(not specified)'}",
            f"Submission ID: {submission.id}",
        ]
        for dimension in rubric.dimensions:
            lines.append(
                f"Rubric Dimension: {dimension.name} | Weight: {dimension.weight:.0%} | Description: {dimension.description}"
            )
        return "\n".join(lines)

    def batch_grade(
        self,
        submissions: List[HomeworkSubmission],
        rubric: Rubric,
        assignment_title: str = "",
    ) -> List[GradingResult]:
        return [self.grade(s, rubric, assignment_title) for s in submissions]
