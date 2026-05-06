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
        self._timeout = int(os.environ.get("LLM_TIMEOUT", "60"))
        self._grading_log_dir = Path("run_logs") / "grading"
        self._grading_log_dir.mkdir(parents=True, exist_ok=True)
        self._rubric_log_dir = Path("run_logs") / "rubrics"
        self._rubric_log_dir.mkdir(parents=True, exist_ok=True)
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
            return (response.choices[0].message.content or "").strip()
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
        prompt = f"""You are an expert instructional designer and grading specialist.

Create a grading rubric for the assignment below. The rubric must be concrete, observable, and directly usable for scoring student work.

## Course
{course_id}

## Assignment Title
{assignment_title}

## Assignment Requirement
{assignment_requirement}

## Previous Draft
{previous_block or "None"}

## Teacher Revision Feedback
{feedback_block}

## Rubric Rules
1. Return JSON only. Do not include markdown fences or commentary outside JSON.
2. Provide 3 to 6 scoring dimensions.
3. Each dimension must have a short, specific, gradeable description.
4. Dimension weights must sum to 1.0.
5. Use Chinese for teacher-facing text.
6. Keep hard rules empty unless the requirement clearly implies objective penalties.
7. Revise the draft when teacher feedback is provided instead of repeating the old version.

## Output JSON Schema
{{
  "title": "评分标准标题",
  "summary": "这一版评分标准如何贴合作业要求的简短说明",
  "teacher_message": "给老师的简短确认提示",
  "dimensions": [
    {{
      "name": "维度名称",
      "weight": 0.35,
      "description": "可直接用于评分的维度说明",
      "max_score": 100,
      "grading_focus": ["2到4个可观察要点"]
    }}
  ],
  "hard_rules": ["可选的客观扣分规则"]
}}"""

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
                            "You are an expert instructional designer. "
                            "Return valid JSON only. "
                            "The rubric must be specific, structured, and directly usable for grading."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=1400,
                timeout=self._timeout,
            )
            raw = (response.choices[0].message.content or "").strip()
            logger.info("AI rubric raw response for title=%s: %s", assignment_title, raw[:3000])
            payload = self._extract_json_payload(raw)
            if not payload:
                raise ValueError("AI did not return parseable JSON for the rubric draft.")

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
            logger.info("AI grading raw response for submission=%s: %s", submission.id, raw[:2000])
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
            fallback_scores = self._extract_scores_from_text(raw, rubric)
            if fallback_scores:
                logger.info(
                    "Recovered non-JSON AI grading output for submission=%s via text parsing",
                    submission.id,
                )
                return GradingResult(
                    submission_id=submission.id,
                    dimension_scores=fallback_scores,
                    total_score=self._calc_weighted_score(fallback_scores, rubric),
                    feedback=raw[:800].strip(),
                    confidence=0.55,
                    graded_by=f"ai:{self._provider}:text-fallback",
                    graded_at=datetime.now(),
                    appeal_count=existing_result.appeal_count if existing_result else 0,
                )
            logger.warning("LLM response was not parseable JSON: %s", raw[:100])
        except Exception as e:
            logger.warning("AI grading failed: %s, falling back to rules", e)

        return self._grade_with_rules(submission, rubric, existing_result=existing_result)

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
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            focus = item.get("grading_focus") or []
            if not isinstance(focus, list):
                focus = [str(focus)]
            normalized_dimensions.append(
                {
                    "name": name,
                    "weight": self._coerce_weight(item.get("weight", 0.0)),
                    "description": str(item.get("description", "")).strip(),
                    "max_score": self._coerce_score(item.get("max_score", 100.0), default=100.0),
                    "grading_focus": [str(entry).strip() for entry in focus if str(entry).strip()],
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
            "title": str(payload.get("title", "")).strip() or f"{fallback_title}评分标准",
            "summary": str(payload.get("summary", "")).strip(),
            "teacher_message": str(payload.get("teacher_message", "")).strip(),
            "dimensions": normalized_dimensions,
            "hard_rules": [
                str(rule).strip()
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
                feedback=(
                    "No readable homework content was found, so the submission "
                    "could not be graded automatically."
                ),
                confidence=0.1,
                graded_by="rule",
                graded_at=datetime.now(),
                appeal_count=existing_result.appeal_count if existing_result else 0,
            )

        dim_scores: Dict[str, float] = {}
        feedback_parts = []
        cleaned_text = re.sub(r"\s+", "", text)
        text_length = len(cleaned_text)
        line_count = len([line for line in text.splitlines() if line.strip()])
        sentence_hits = len(re.findall(r"[???!?.;?]", text))

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
            if any(keyword in name_lower for keyword in ("content", "accuracy")):
                if any(kw in text for kw in ["??", "??", "??", "??", "??", "??"]):
                    score += 12
            if "logic" in name_lower:
                if any(kw in text for kw in ["??", "??", "??", "??", "??", "??", "??"]):
                    score += 12
            if any(keyword in name_lower for keyword in ("expression", "language")):
                if text_length >= 100:
                    score += 10

            score = min(score, 100)
            dim_scores[d.name] = round(score, 1)
            feedback_parts.append(f"{d.name}: {score}")

        total = self._calc_weighted_score(dim_scores, rubric)
        feedback = "; ".join(feedback_parts) + " (rule-based fallback)"

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

    def _has_readable_submission_text(self, text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False

        unreadable_markers = (
            "[file submission]",
            "[PDF parser unavailable",
            "[DOCX parser unavailable",
            "[Unable to decode file:",
            "[PDF?????",
            "[DOCX?????",
            "[??????:",
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

    def _coerce_weight(self, value: Any, default: float = 0.0) -> float:
        try:
            weight = float(value)
        except (TypeError, ValueError):
            return default
        if weight > 1.0:
            weight /= 100.0
        return round(min(max(weight, 0.0), 1.0), 4)

    def batch_grade(
        self,
        submissions: List[HomeworkSubmission],
        rubric: Rubric,
        assignment_title: str = "",
    ) -> List[GradingResult]:
        return [self.grade(s, rubric, assignment_title) for s in submissions]
