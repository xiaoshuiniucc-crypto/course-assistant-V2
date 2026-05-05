"""
M11 — 申诉处理
管理申诉的完整生命周期：接收 → AI 重评 → 助教审核 → 终态确认。
MVP 阶段助教审核用控制台输入模拟。
"""

import json
from datetime import datetime
from typing import Optional

from contracts.models import AppealRecord, GradingResult, Submission
from storage import db
from grading.engine import regrade_with_appeal, LLMClient
from homework.receiver import get_submission_text
from notify.result_notifier import format_summary


def submit_appeal(submission_id: str, student_qq: str,
                  reason: str) -> AppealRecord:
    """学生提交申诉。"""
    sub = db.get_submission(submission_id)
    if not sub:
        raise ValueError(f"提交记录不存在：{submission_id}")
    if sub.status != "graded":
        raise ValueError(f"当前状态「{sub.status}」不可申诉，仅已批改的作业可申诉")
    if sub.score is None:
        raise ValueError("批改分数为空，无法申诉")

    # 创建申诉记录
    record = AppealRecord(
        id=None,
        submission_id=submission_id,
        student_qq=student_qq,
        student_reason=reason,
        original_score=sub.score,
        ai_new_score=None,
        regrade_detail=None,
        ta_decision=None,
        ta_note=None,
        status="APPEALING",
        created_at=datetime.now(),
        decided_at=None,
    )

    appeal_id = db.create_appeal(record)
    record.id = appeal_id
    return record


def trigger_regrade(appeal_id: int, rubric, courseware_context: str,
                    llm_client: LLMClient) -> GradingResult:
    """AI 重新评分。"""
    appeal = db.get_appeal(appeal_id)
    if not appeal:
        raise ValueError(f"申诉记录不存在：{appeal_id}")

    sub = db.get_submission(appeal.submission_id)
    submission_text = get_submission_text(appeal.submission_id)

    # 获取原始批改结果（简化：用当前分数构造）
    original_result = GradingResult(
        submission_id=appeal.submission_id,
        total_score=appeal.original_score,
        dimensions=[],
        confidence="medium",
        grading_context="",
    )

    # 重评
    new_result = regrade_with_appeal(
        submission_text=submission_text,
        original_result=original_result,
        rubric=rubric,
        appeal_reason=appeal.student_reason,
        courseware_context=courseware_context,
        llm_client=llm_client,
    )

    # 更新申诉记录
    diff = {
        "original_score": appeal.original_score,
        "new_score": new_result.total_score,
        "change": new_result.total_score - appeal.original_score,
        "dimensions": [
            {"name": d.name, "score": d.score, "max_score": d.max_score,
             "comment": d.dimension_comment}
            for d in new_result.dimensions
        ],
    }
    db.update_appeal_regrade(appeal_id, new_result.total_score, json.dumps(diff, ensure_ascii=False))

    return new_result


def resolve_appeal_interactive(appeal_id: int, regrade_result: GradingResult,
                                original_score: int) -> None:
    """
    交互式助教审核（MVP 用控制台模拟）。
    真实版本中替换为 QQ 频道消息。
    """
    print("\n" + "─" * 50)
    print("【申诉待审】")
    print(f"  原分：{original_score} → AI重评：{regrade_result.total_score}（{regrade_result.total_score - original_score:+d}分）")
    print(f"  置信度：{regrade_result.confidence}")
    print("")
    print("  AI重评分项：")
    for dim in regrade_result.dimensions:
        print(f"    · {dim.name}：{dim.score}/{dim.max_score} — {dim.dimension_comment}")
    print("─" * 50)

    while True:
        decision = input("\n助教操作 → 输入「同意」批准重评分 / 输入「驳回 原因」维持原分：").strip()
        if decision in ("同意", "1", "批准"):
            note = input("备注（可留空）：").strip()
            new_score = regrade_result.total_score
            db.resolve_appeal(appeal_id, "approved", note or "同意AI重评结果", new_score)
            print(f"✅ 已批准，新分数 {new_score} 分已生效。")
            return
        elif decision.startswith("驳回") or decision.startswith("2"):
            note = decision.replace("驳回", "").replace("2", "").strip()
            if not note:
                note = input("请输入驳回原因：").strip()
            db.resolve_appeal(appeal_id, "rejected", note, original_score)
            print(f"❌ 已驳回，维持原分 {original_score} 分。")
            return
        else:
            print("输入无效，请重试。")


def get_appeal_summary(appeal_id: int) -> str:
    """获取申诉摘要文本。"""
    appeal = db.get_appeal(appeal_id)
    if not appeal:
        return "申诉记录不存在"

    lines = [
        f"【申诉结果】",
        f"  原始分数：{appeal.original_score}",
        f"  AI重评分数：{appeal.ai_new_score}",
    ]
    if appeal.ta_decision == "approved":
        lines.append(f"  助教决定：✅ 批准重评 → 最终分数：{appeal.ai_new_score}")
    elif appeal.ta_decision == "rejected":
        lines.append(f"  助教决定：❌ 驳回 → 维持原分：{appeal.original_score}")
        lines.append(f"  驳回原因：{appeal.ta_note}")
    else:
        lines.append(f"  当前状态：{appeal.status}")

    return "\n".join(lines)
