"""
M12 — PDF 报告生成
按需生成详细的 PDF 批改报告，包含完整的得分点、扣分点和课件依据。

MVP 策略：
- 使用 reportlab 生成 PDF（纯 Python，无外部依赖）
- 如果 reportlab 不可用，回退到生成 HTML 报告
- 懒加载：批改完成时不自动生成，学生请求时才生成
- 缓存策略：生成后缓存路径，申诉重评后使缓存失效

PDF 内容结构：
1. 封面：课程名、作业名、学生信息、提交时间、批改时间
2. 总分概览：各维度得分率
3. 分项详情：得分点/扣分点/引用依据
4. 申诉记录（如有）
5. 附录：完整评分细则
"""

import os
import json
from datetime import datetime
from typing import Optional
from pathlib import Path

from contracts.models import GradingResult, AppealRecord, Rubric
from storage import db


# 报告输出目录
REPORT_DIR = Path(__file__).parent.parent / "data" / "reports"


class ReportGenerator:
    """
    PDF 报告生成器。
    - generate_pdf(): 生成 PDF 报告
    - get_or_generate(): 懒加载：有缓存直接返回
    - invalidate_cache(): 使缓存失效
    """

    def __init__(self):
        self._cache: dict[str, str] = {}  # {submission_id: file_path}
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        self._use_reportlab = self._check_reportlab()

    def _check_reportlab(self) -> bool:
        """检查 reportlab 是否可用。"""
        try:
            from reportlab.lib.pagesizes import A4
            return True
        except ImportError:
            return False

    # ── 公开接口 ──────────────────────────────────────────

    def generate_pdf(self, grading_result: GradingResult,
                     course_name: str = "",
                     assignment_name: str = "",
                     student_qq: str = "",
                     rubric: Optional[Rubric] = None,
                     appeal_record: Optional[AppealRecord] = None) -> str:
        """
        生成 PDF 报告，返回文件路径。
        """
        if self._use_reportlab:
            return self._generate_with_reportlab(
                grading_result, course_name, assignment_name, student_qq, rubric, appeal_record
            )
        else:
            return self._generate_html(
                grading_result, course_name, assignment_name, student_qq, rubric, appeal_record
            )

    def get_or_generate(self, submission_id: str,
                        course_name: str = "",
                        assignment_name: str = "",
                        student_qq: str = "") -> str:
        """
        懒加载：有缓存直接返回，否则生成新的。
        """
        # 检查缓存
        if submission_id in self._cache:
            cached_path = self._cache[submission_id]
            if os.path.exists(cached_path):
                return cached_path

        # 从数据库获取数据
        sub = db.get_submission(submission_id)
        if not sub:
            raise ValueError(f"提交记录不存在：{submission_id}")

        grading_result = self._get_grading_result(submission_id)
        rubric = db.get_latest_rubric(sub.assignment_id)
        appeal = self._get_latest_appeal(submission_id)

        return self.generate_pdf(
            grading_result=grading_result,
            course_name=course_name,
            assignment_name=assignment_name or sub.assignment_id,
            student_qq=student_qq or sub.student_qq,
            rubric=rubric,
            appeal_record=appeal,
        )

    def invalidate_cache(self, submission_id: str) -> None:
        """使缓存失效（申诉重评后调用）。"""
        if submission_id in self._cache:
            old_path = self._cache.pop(submission_id)
            if os.path.exists(old_path):
                os.remove(old_path)

    # ── ReportLab 生成 ─────────────────────────────────────

    def _generate_with_reportlab(self, grading_result, course_name, assignment_name,
                                  student_qq, rubric, appeal_record) -> str:
        """使用 ReportLab 生成 PDF。"""
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        # 注册中文字体（尝试系统字体）
        font_registered = False
        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",   # 微软雅黑
            "C:/Windows/Fonts/simhei.ttf",  # 黑体
            "C:/Windows/Fonts/simsun.ttc",  # 宋体
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    pdfmetrics.registerFont(TTFont('ChineseFont', fp))
                    font_registered = True
                    break
                except Exception:
                    continue

        font_name = 'ChineseFont' if font_registered else 'Helvetica'

        output_path = REPORT_DIR / f"report_{grading_result.submission_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"

        doc = SimpleDocTemplate(
            str(output_path), pagesize=A4,
            leftMargin=25*mm, rightMargin=25*mm,
            topMargin=20*mm, bottomMargin=20*mm,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('CustomTitle', parent=styles['Title'],
                                      fontName=font_name, fontSize=18, spaceAfter=20)
        heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'],
                                        fontName=font_name, fontSize=14, spaceAfter=10)
        body_style = ParagraphStyle('CustomBody', parent=styles['Normal'],
                                     fontName=font_name, fontSize=10, leading=16)

        elements = []

        # 1. 封面
        elements.append(Paragraph(f"批改报告", title_style))
        elements.append(Spacer(1, 10))
        info_text = f"课程：{course_name}<br/>作业：{assignment_name}<br/>学生QQ：{student_qq}<br/>提交时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        elements.append(Paragraph(info_text, body_style))
        elements.append(Spacer(1, 20))

        # 2. 总分概览
        elements.append(Paragraph("总分概览", heading_style))
        total_text = f"总分：{grading_result.total_score} 分"
        if rubric:
            total_text += f" / {rubric.total_score} 分"
        total_text += f"&nbsp;&nbsp;&nbsp;置信度：{grading_result.confidence}"
        elements.append(Paragraph(total_text, body_style))
        elements.append(Spacer(1, 10))

        # 维度得分表格
        table_data = [["维度", "得分", "满分", "得分率"]]
        for dim in grading_result.dimensions:
            rate = f"{dim.score/dim.max_score*100:.0f}%" if dim.max_score > 0 else "N/A"
            table_data.append([dim.name, str(dim.score), str(dim.max_score), rate])

        table = Table(table_data, colWidths=[120, 60, 60, 60])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, -1), font_name),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(table)
        elements.append(Spacer(1, 20))

        # 3. 分项详情
        elements.append(Paragraph("分项详情", heading_style))
        for dim in grading_result.dimensions:
            elements.append(Spacer(1, 8))
            elements.append(Paragraph(f"<b>{dim.name}</b> ({dim.score}/{dim.max_score})", body_style))

            if dim.gain_points:
                elements.append(Paragraph("&nbsp;&nbsp;✅ 得分点：", body_style))
                for gp in dim.gain_points:
                    elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;· {gp}", body_style))

            if dim.deductions:
                elements.append(Paragraph("&nbsp;&nbsp;❌ 扣分点：", body_style))
                for dd in dim.deductions:
                    elements.append(Paragraph(
                        f"&nbsp;&nbsp;&nbsp;&nbsp;· {dd.point}（-{dd.deduct}分）"
                        f"<br/>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;依据：{dd.evidence}（{dd.evidence_source}）",
                        body_style
                    ))

            elements.append(Paragraph(f"&nbsp;&nbsp;评语：{dim.dimension_comment}", body_style))

        # 4. 申诉记录
        if appeal_record:
            elements.append(Spacer(1, 20))
            elements.append(Paragraph("申诉记录", heading_style))
            appeal_text = f"申诉理由：{appeal_record.student_reason}"
            if appeal_record.ai_new_score is not None:
                appeal_text += f"<br/>AI重评分数：{appeal_record.ai_new_score}"
            if appeal_record.ta_decision:
                appeal_text += f"<br/>助教决定：{'批准' if appeal_record.ta_decision == 'approved' else '驳回'}"
            if appeal_record.ta_note:
                appeal_text += f"<br/>助教备注：{appeal_record.ta_note}"
            elements.append(Paragraph(appeal_text, body_style))

        # 5. 附录：评分细则
        if rubric:
            elements.append(Spacer(1, 20))
            elements.append(Paragraph("附录：评分细则", heading_style))
            for dim in rubric.dimensions:
                elements.append(Paragraph(
                    f"· {dim.name}（{dim.max_score}分）：{dim.criteria}",
                    body_style
                ))
            for rule in rubric.hard_deductions:
                elements.append(Paragraph(
                    f"· 硬性扣分：{rule.condition}（{rule.penalty}分）",
                    body_style
                ))

        doc.build(elements)
        self._cache[grading_result.submission_id] = str(output_path)
        return str(output_path)

    # ── HTML 回退生成 ─────────────────────────────────────

    def _generate_html(self, grading_result, course_name, assignment_name,
                        student_qq, rubric, appeal_record) -> str:
        """回退：生成 HTML 报告。"""
        output_path = REPORT_DIR / f"report_{grading_result.submission_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.html"

        # 构建维度得分行
        dim_rows = ""
        for dim in grading_result.dimensions:
            rate = f"{dim.score/dim.max_score*100:.0f}%" if dim.max_score > 0 else "N/A"
            bar_width = (dim.score / dim.max_score * 100) if dim.max_score > 0 else 0
            color = "#4CAF50" if bar_width >= 70 else "#FF9800" if bar_width >= 50 else "#F44336"

            gain_list = "".join(f"<li>{gp}</li>" for gp in dim.gain_points)
            deduct_list = ""
            for dd in dim.deductions:
                deduct_list += f"""<li><span class="deduct">-{dd.deduct}分</span> {dd.point}
                    <div class="evidence">依据：{dd.evidence}（{dd.evidence_source}）</div></li>"""

            dim_rows += f"""
            <div class="dimension-card">
                <div class="dim-header">
                    <span class="dim-name">{dim.name}</span>
                    <span class="dim-score">{dim.score}/{dim.max_score}</span>
                </div>
                <div class="progress-bar"><div class="progress-fill" style="width:{bar_width}%;background:{color}"></div></div>
                <p class="dim-rate">得分率：{rate}</p>
                {"<div class='section'><h4>✅ 得分点</h4><ul>" + gain_list + "</ul></div>" if gain_list else ""}
                {"<div class='section'><h4>❌ 扣分点</h4><ul>" + deduct_list + "</ul></div>" if deduct_list else ""}
                <p class="comment">评语：{dim.dimension_comment}</p>
            </div>"""

        # 申诉记录
        appeal_section = ""
        if appeal_record:
            appeal_section = f"""
            <div class="section">
                <h2>申诉记录</h2>
                <p>申诉理由：{appeal_record.student_reason}</p>
                <p>AI重评分数：{appeal_record.ai_new_score or '待重评'}</p>
                <p>助教决定：{'批准' if appeal_record.ta_decision == 'approved' else '驳回' if appeal_record.ta_decision else '待审核'}</p>
            </div>"""

        # 评分细则
        rubric_section = ""
        if rubric:
            rubric_dims = "".join(f"<li>{d.name}（{d.max_score}分）：{d.criteria}</li>" for d in rubric.dimensions)
            rubric_rules = "".join(f"<li>{r.condition}（{r.penalty}分）</li>" for r in rubric.hard_deductions)
            rubric_section = f"""
            <div class="section">
                <h2>附录：评分细则</h2>
                <ul>{rubric_dims}</ul>
                <h4>硬性扣分项</h4>
                <ul>{rubric_rules}</ul>
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>批改报告 - {assignment_name}</title>
<style>
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; color: #333; }}
h1 {{ color: #1a1a2e; border-bottom: 3px solid #4472C4; padding-bottom: 10px; }}
h2 {{ color: #4472C4; margin-top: 30px; }}
.header-info {{ background: #f0f4ff; padding: 15px; border-radius: 8px; margin: 15px 0; }}
.total-score {{ font-size: 2em; color: #4472C4; font-weight: bold; }}
.confidence {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.85em; }}
.conf-high {{ background: #e8f5e9; color: #2e7d32; }}
.conf-medium {{ background: #fff3e0; color: #ef6c00; }}
.conf-low {{ background: #ffebee; color: #c62828; }}
.dimension-card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 15px; margin: 15px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.dim-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.dim-name {{ font-weight: bold; font-size: 1.1em; }}
.dim-score {{ font-size: 1.1em; color: #4472C4; }}
.progress-bar {{ background: #e0e0e0; border-radius: 4px; height: 8px; overflow: hidden; }}
.progress-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
.dim-rate {{ font-size: 0.9em; color: #666; margin: 5px 0; }}
.section {{ margin-top: 10px; }}
.section h4 {{ margin: 5px 0; }}
.section ul {{ padding-left: 20px; }}
.deduct {{ color: #c62828; font-weight: bold; }}
.evidence {{ color: #666; font-size: 0.9em; margin-left: 15px; }}
.comment {{ background: #f5f5f5; padding: 8px; border-radius: 4px; margin-top: 8px; }}
</style>
</head>
<body>
<h1>批改报告</h1>
<div class="header-info">
<p>课程：{course_name}</p>
<p>作业：{assignment_name}</p>
<p>学生QQ：{student_qq}</p>
<p>生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>

<div class="total-score">
    总分：{grading_result.total_score}{" / " + str(rubric.total_score) if rubric else ""} 分
    <span class="confidence conf-{grading_result.confidence}">{grading_result.confidence}</span>
</div>

<h2>分项详情</h2>
{dim_rows}

{appeal_section}
{rubric_section}

</body>
</html>"""

        output_path.write_text(html, encoding="utf-8")
        self._cache[grading_result.submission_id] = str(output_path)
        return str(output_path)

    # ── 辅助方法 ──────────────────────────────────────────

    def _get_grading_result(self, submission_id: str) -> GradingResult:
        """从数据库获取批改结果。"""
        conn = db._get_conn()
        row = conn.execute(
            "SELECT * FROM grading_details WHERE submission_id = ? ORDER BY created_at DESC LIMIT 1",
            (submission_id,)
        ).fetchone()
        conn.close()

        if not row:
            raise ValueError(f"批改结果不存在：{submission_id}")

        dimensions = []
        for d in json.loads(row["dimension_json"]):
            deductions = [Deduction(
                point=dd["point"], deduct=dd["deduct"],
                evidence=dd["evidence"], evidence_source=dd["evidence_source"],
            ) for dd in d.get("deductions", [])]
            dimensions.append(DimensionResult(
                name=d["name"], score=d["score"], max_score=d["max_score"],
                gain_points=d.get("gain_points", []),
                deductions=deductions,
                dimension_comment=d.get("dimension_comment", ""),
            ))

        return GradingResult(
            submission_id=submission_id,
            total_score=sum(d.score for d in dimensions),
            dimensions=dimensions,
            confidence=row["confidence"],
            grading_context=row["grading_context"] or "",
        )

    def _get_latest_appeal(self, submission_id: str) -> Optional[AppealRecord]:
        """从数据库获取最近的申诉记录。"""
        conn = db._get_conn()
        row = conn.execute(
            "SELECT * FROM appeals WHERE submission_id = ? ORDER BY created_at DESC LIMIT 1",
            (submission_id,)
        ).fetchone()
        conn.close()

        if not row:
            return None

        return AppealRecord(
            id=row["id"], submission_id=row["submission_id"],
            student_qq=row["student_qq"], student_reason=row["student_reason"],
            original_score=row["original_score"], ai_new_score=row["ai_new_score"],
            regrade_detail=row["regrade_detail"],
            ta_decision=row["ta_decision"], ta_note=row["ta_note"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        )
