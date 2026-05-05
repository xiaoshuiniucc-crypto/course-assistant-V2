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
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv

    _env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    load_dotenv(_env_path)
except ImportError:
    pass

from contracts.models import GradingResult, HomeworkSubmission, Rubric
from storage.db import DB

logger = logging.getLogger("grading")


class GradingEngine:
    """AI grading engine with multiple providers and a rules fallback."""

    def __init__(self, db: DB, model: str = None):
        self.db = db
        self._client = None
        self._timeout = int(os.environ.get("LLM_TIMEOUT", "60"))
        self._provider, self.model, self._api_key, self._base_url = self._resolve_config(
            model
        )
        logger.info(
            "Grading engine configured: provider=%s, model=%s",
            self._provider,
            self.model,
        )

    @staticmethod
    def _resolve_config(model_override: str = None):
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
        ds_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        ds_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        if ds_key:
            return "deepseek", (model_override or ds_model), ds_key, ds_url

        oa_key = os.environ.get("OPENAI_API_KEY", "")
        oa_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        oa_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        if oa_key:
            return "openai", (model_override or oa_model), oa_key, oa_url

        return "rule", "rule-engine", "", None

    @property
    def client(self):
        if self._client is None and self._api_key:
            try:
                from openai import OpenAI

                kwargs = {"api_key": self._api_key}
                if self._base_url:
                    kwargs["base_url"] = self._base_url
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

        from contracts.models import SubmitStatus

        submission.score = result.total_score
        submission.feedback = result.feedback
        submission.graded_at = datetime.now()
        submission.status = SubmitStatus.GRADED
        self.db.save_submission(submission)
        return result

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

    def _grade_with_ai(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        assignment_title: str,
        appeal_reason: str = "",
        existing_result: Optional[GradingResult] = None,
    ) -> GradingResult:
        dim_desc = "\n".join(
            f"- {d.name} (weight {d.weight:.0%}): {d.description or 'No description'}"
            for d in rubric.dimensions
        )

        appeal_note = ""
        if appeal_reason:
            appeal_note = (
                f"\n\n[Appeal Context]\nStudent appeal reason: {appeal_reason}\n"
                "Re-evaluate the work and adjust scores if the appeal is justified."
            )

        prompt = f"""You are a professional course TA. Grade the student's work by rubric dimensions.

## Assignment
{assignment_title or '(not specified)'}

## Rubric
{dim_desc}

## Student Submission
{submission.content[:3000]}
{appeal_note}

## Output Requirement
Return JSON only:
{{
  "dimension_scores": {{
    "dimension1": 0,
    "dimension2": 0
  }},
  "feedback": "overall feedback and improvement suggestions",
  "confidence": 0.0
}}"""

        try:
            logger.info(
                "Calling %s model=%s for submission=%s",
                self._provider,
                self.model,
                submission.id,
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a professional course TA. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1000,
                timeout=self._timeout,
            )
            raw = response.choices[0].message.content.strip()
            logger.debug("LLM raw response: %s", raw[:200])
            data = self._extract_json_payload(raw)
            if data:
                dim_scores = self._normalize_dimension_scores(
                    data.get("dimension_scores", {}),
                    rubric,
                )
                return GradingResult(
                    submission_id=submission.id,
                    dimension_scores=dim_scores,
                    total_score=self._calc_weighted_score(dim_scores, rubric),
                    feedback=str(data.get("feedback", "")).strip(),
                    confidence=self._coerce_confidence(data.get("confidence", 0.7)),
                    graded_by=f"ai:{self._provider}",
                    graded_at=datetime.now(),
                    appeal_count=existing_result.appeal_count if existing_result else 0,
                )
            logger.warning("LLM response was not parseable JSON: %s", raw[:100])
        except Exception as e:
            logger.warning("AI grading failed: %s, falling back to rules", e)

        return self._grade_with_rules(submission, rubric, existing_result=existing_result)

    def _grade_with_rules(
        self,
        submission: HomeworkSubmission,
        rubric: Rubric,
        existing_result: Optional[GradingResult] = None,
    ) -> GradingResult:
        text = submission.content
        dim_scores: Dict[str, float] = {}
        feedback_parts = []

        for d in rubric.dimensions:
            score = 65.0
            if len(text) > 500:
                score += 10
            elif len(text) > 200:
                score += 5

            name_lower = d.name.lower()
            if "content" in name_lower or "accuracy" in name_lower:
                if any(kw in text for kw in ["正确", "准确", "分析", "结论"]):
                    score += 10
            if "logic" in name_lower:
                if any(kw in text for kw in ["因此", "所以", "因为", "首先", "其次"]):
                    score += 10
            if "expression" in name_lower or "language" in name_lower:
                if len(text) > 100:
                    score += 8

            score = min(score, 100)
            dim_scores[d.name] = round(score, 1)
            feedback_parts.append(f"{d.name}: {score}")

        total = self._calc_weighted_score(dim_scores, rubric)
        feedback = "；".join(feedback_parts) + "。 (rule-based fallback)"

        return GradingResult(
            submission_id=submission.id,
            dimension_scores=dim_scores,
            total_score=round(total, 1),
            feedback=feedback,
            confidence=0.5,
            graded_by="rule",
            graded_at=datetime.now(),
            appeal_count=existing_result.appeal_count if existing_result else 0,
        )

    def _apply_hard_rules(
        self,
        result: GradingResult,
        rubric: Rubric,
        submission: HomeworkSubmission,
    ) -> GradingResult:
        deductions = []
        for rule in rubric.hard_rules:
            match = re.search(r"(\d+)", rule)
            if match:
                pts = int(match.group(1))
                result.total_score -= pts
                deductions.append(f"Hard deduction: {rule} (-{pts})")

        if deductions:
            result.feedback += "\n" + "\n".join(deductions)

        result.total_score = max(0, round(result.total_score, 1))
        return result

    def _calc_weighted_score(self, dim_scores: Dict[str, float], rubric: Rubric) -> float:
        total = 0.0
        for d in rubric.dimensions:
            total += dim_scores.get(d.name, 0) * d.weight
        return round(total, 1)

    def _extract_json_payload(self, raw: str) -> Optional[Dict[str, Any]]:
        cleaned = raw.strip()
        fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
        if fenced_match:
            cleaned = fenced_match.group(1).strip()

        candidates = [cleaned]
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            candidates.append(match.group())

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _normalize_dimension_scores(self, raw_scores: Any, rubric: Rubric) -> Dict[str, float]:
        normalized_scores: Dict[str, float] = {}
        if isinstance(raw_scores, dict):
            for raw_name, raw_value in raw_scores.items():
                matched_name = self._match_dimension_name(str(raw_name), rubric)
                score = self._coerce_score(raw_value, default=70.0)
                if matched_name:
                    normalized_scores[matched_name] = score

        for dimension in rubric.dimensions:
            normalized_scores.setdefault(dimension.name, 70.0)

        return normalized_scores

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

    def batch_grade(
        self,
        submissions: List[HomeworkSubmission],
        rubric: Rubric,
        assignment_title: str = "",
    ) -> List[GradingResult]:
        return [self.grade(s, rubric, assignment_title) for s in submissions]
