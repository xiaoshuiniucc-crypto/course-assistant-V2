"""
M08 — 批改结果通知
将 GradingResult 格式化为可读的文字摘要。
MVP 阶段输出到控制台（模拟私聊发送）。
"""

from contracts.models import GradingResult, DimensionResult, Deduction


def format_summary(result: GradingResult, course_name: str = "",
                   assignment_title: str = "") -> str:
    """将批改结果格式化为文字摘要。"""
    lines = []

    # 标题
    title_parts = [s for s in [course_name, assignment_title] if s]
    header = " - ".join(title_parts) if title_parts else "作业批改"
    lines.append(f"【作业批改结果】{header}")
    lines.append("")

    # 总分
    max_score = sum(d.max_score for d in result.dimensions if d.max_score > 0)
    lines.append(f"总分：{result.total_score} / {max_score}")
    lines.append(f"置信度：{result.confidence}")
    if result.confidence == "low":
        lines.append("⚠️ 置信度较低，建议联系助教复核")
    lines.append("")

    # 分项得分
    lines.append("分项得分：")
    for dim in result.dimensions:
        if dim.max_score > 0:
            diff = dim.max_score - dim.score
            indicator = f" ▼ 扣{diff}分" if diff > 0 else " ✓ 满分"
            lines.append(f"  · {dim.name}    {dim.score}/{dim.max_score}{indicator}")
    lines.append("")

    # 扣分详情
    all_deductions: list[tuple[str, Deduction]] = []
    for dim in result.dimensions:
        for dd in dim.deductions:
            all_deductions.append((dim.name, dd))

    if all_deductions:
        lines.append("主要扣分原因：")
        for i, (dim_name, dd) in enumerate(all_deductions[:5], 1):
            lines.append(f"  {i}. [{dim_name}] {dd.point}（-{dd.deduct}分）")
            if dd.evidence:
                lines.append(f"     依据：{dd.evidence}（{dd.evidence_source}）")
        lines.append("")

    # 得分点
    has_gains = any(dim.gain_points for dim in result.dimensions)
    if has_gains:
        lines.append("得分亮点：")
        for dim in result.dimensions:
            for gp in dim.gain_points:
                lines.append(f"  ✓ [{dim.name}] {gp}")
        lines.append("")

    # 分项评语
    lines.append("分项评语：")
    for dim in result.dimensions:
        lines.append(f"  【{dim.name}】{dim.dimension_comment}")
    lines.append("")

    # 操作提示
    lines.append("───")
    lines.append("如需查看完整批改报告，回复「报告」")
    lines.append("如有异议，回复「申诉」并说明理由。")

    return "\n".join(lines)


def print_result(result: GradingResult, course_name: str = "",
                 assignment_title: str = "") -> None:
    """格式化并打印批改结果（模拟私聊发送）。"""
    summary = format_summary(result, course_name, assignment_title)
    print("\n" + "=" * 50)
    print(summary)
    print("=" * 50 + "\n")
