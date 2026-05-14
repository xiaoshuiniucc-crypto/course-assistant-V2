"""
Helpers for turning structured grading results into readable evidence text.
"""
from __future__ import annotations

from typing import List, Optional

from contracts.models import GradingResult, Rubric


def build_grading_evidence(
    result: GradingResult,
    rubric: Optional[Rubric] = None,
) -> List[str]:
    lines: List[str] = []
    if not result or not result.dimension_scores:
        return lines

    rubric_dimensions = {d.name: d for d in rubric.dimensions} if rubric else {}

    for dim_name, score in result.dimension_scores.items():
        dimension = rubric_dimensions.get(dim_name)
        parts = [f"{dim_name}: {score:.1f}"]

        if dimension:
            parts.append(f"权重 {dimension.weight:.0%}")
            if dimension.description:
                parts.append(dimension.description[:90])

        gain_points = (result.gain_points or {}).get(dim_name, [])
        if gain_points:
            parts.append("得分点: " + "；".join(gain_points[:2]))

        deductions = (result.deductions or {}).get(dim_name, [])
        if deductions:
            deduction_parts = []
            for item in deductions[:2]:
                point = str(item.get("point", "")).strip() or "存在扣分项"
                deduct = item.get("deduct", 0)
                evidence_source = str(item.get("evidence_source", "")).strip()
                evidence = str(item.get("evidence", "")).strip()
                chunk = point
                if deduct:
                    chunk += f" (-{deduct})"
                if evidence_source:
                    chunk += f"；来源: {evidence_source}"
                if evidence:
                    chunk += f"；依据: {evidence[:60]}"
                deduction_parts.append(chunk)
            parts.append("扣分点: " + " | ".join(deduction_parts))

        lines.append("；".join(parts))

    return lines


def build_grading_evidence_text(
    result: GradingResult,
    rubric: Optional[Rubric] = None,
    *,
    limit: Optional[int] = None,
) -> str:
    lines = build_grading_evidence(result, rubric)
    if limit is not None:
        lines = lines[:limit]
    return "\n".join(lines)
