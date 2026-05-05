"""
报告生成器
ChromaDB → 检索 → 报告
支持 reportlab PDF 输出 / HTML fallback
"""
from __future__ import annotations
import os
import json
import hashlib
import logging
from datetime import datetime
from typing import Optional, Dict, List
from pathlib import Path

from storage.db import DB

logger = logging.getLogger("report")


class ReportGenerator:
    """
    批改报告生成器
    - reportlab PDF 输出（含中文字体支持）
    - HTML fallback
    - 惰性缓存 + 失效机制
    """

    def __init__(self, db: DB, output_dir: str = "reports"):
        self.db = db
        self.output_dir = output_dir
        self._cache: Dict[str, str] = {}  # cache_key → file_path
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    def get_or_generate(self, assignment_id: str,
                        force: bool = False) -> str:
        """
        惰性获取报告（带缓存）
        Returns: 报告文件路径
        """
        cache_key = self._cache_key(assignment_id)

        if not force and cache_key in self._cache:
            path = self._cache[cache_key]
            if os.path.exists(path):
                logger.info(f"报告缓存命中: {assignment_id}")
                return path

        # 检查磁盘缓存
        if not force:
            disk_path = self._find_cached_report(assignment_id)
            if disk_path:
                self._cache[cache_key] = disk_path
                return disk_path

        # 生成报告
        path = self.generate(assignment_id)
        self._cache[cache_key] = path
        return path

    def generate(self, assignment_id: str) -> str:
        """生成报告"""
        data = self._collect_data(assignment_id)
        if not data["submissions"]:
            # 无数据 → 生成空报告
            data["submissions"] = []

        # 尝试 PDF
        path = self._generate_pdf(data)
        if path:
            return path

        # fallback: HTML
        return self._generate_html(data)

    def invalidate(self, assignment_id: str):
        """失效缓存"""
        cache_key = self._cache_key(assignment_id)
        if cache_key in self._cache:
            del self._cache[cache_key]

    # ── 数据收集 ────────────────────────────

    def _collect_data(self, assignment_id: str) -> Dict:
        """收集报告数据"""
        assignment = self.db.get_assignment(assignment_id)
        submissions = self.db.list_submissions(assignment_id)

        rows = []
        for s in submissions:
            result = self.db.get_grading_result(s.id)
            row = {
                "student_id": s.student_id,
                "student_name": s.student_name,
                "status": s.status,
                "score": s.score,
                "feedback": s.feedback or "",
                "plagiarized": s.plagiarized,
                "appeal_status": s.appeal_status,
                "submitted_at": s.submitted_at.isoformat(),
            }
            if result:
                row["dimension_scores"] = result.dimension_scores
                row["confidence"] = result.confidence
            rows.append(row)

        scores = [s.score for s in submissions if s.score is not None]
        rubric = None
        if assignment and assignment.rubric_id:
            rubric = self.db.get_rubric(assignment.rubric_id)

        return {
            "assignment_id": assignment_id,
            "assignment_title": assignment.title if assignment else assignment_id,
            "generated_at": datetime.now().isoformat(),
            "total_submissions": len(submissions),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "max_score": max(scores) if scores else 0,
            "min_score": min(scores) if scores else 0,
            "rubric_dimensions": (
                [d.name for d in rubric.dimensions]
                if rubric else []
            ),
            "submissions": rows,
        }

    # ── PDF 生成 (reportlab) ────────────────

    def _generate_pdf(self, data: Dict) -> Optional[str]:
        """使用 reportlab 生成 PDF"""
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
            )
            from reportlab.lib import colors
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
        except ImportError:
            logger.warning("reportlab 未安装, fallback到HTML")
            return None

        # 注册中文字体
        font_name = self._register_chinese_font()

        output_path = os.path.join(
            self.output_dir,
            f"report_{data['assignment_id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        )

        doc = SimpleDocTemplate(output_path, pagesize=A4)
        styles = getSampleStyleSheet()

        # 中文字体样式
        cn_title = ParagraphStyle(
            "CNTitle", parent=styles["Title"],
            fontName=font_name, fontSize=18,
        )
        cn_normal = ParagraphStyle(
            "CNNormal", parent=styles["Normal"],
            fontName=font_name, fontSize=10,
        )
        cn_heading = ParagraphStyle(
            "CNHeading", parent=styles["Heading2"],
            fontName=font_name, fontSize=14,
        )

        elements = []

        # 标题
        elements.append(Paragraph(
            f"批改报告 - {data['assignment_title']}", cn_title
        ))
        elements.append(Spacer(1, 10 * mm))

        # 概要
        summary_text = (
            f"提交数: {data['total_submissions']} | "
            f"平均分: {data['avg_score']} | "
            f"最高分: {data['max_score']} | "
            f"最低分: {data['min_score']}"
        )
        elements.append(Paragraph(summary_text, cn_normal))
        elements.append(Spacer(1, 5 * mm))

        # 评分维度
        if data["rubric_dimensions"]:
            dim_text = "评分维度: " + ", ".join(data["rubric_dimensions"])
            elements.append(Paragraph(dim_text, cn_normal))
            elements.append(Spacer(1, 5 * mm))

        # 学生成绩表
        elements.append(Paragraph("学生成绩明细", cn_heading))
        elements.append(Spacer(1, 3 * mm))

        # 表头
        headers = ["学生", "状态", "分数", "抄袭", "申诉"]
        table_data = [headers]

        for row in data["submissions"]:
            score = f"{row['score']:.1f}" if row['score'] is not None else "-"
            plag = "⚠️" if row["plagiarized"] else ""
            appeal = {
                "none": "", "pending": "待处理",
                "approved": "已批准", "rejected": "已驳回"
            }.get(row["appeal_status"], "")
            table_data.append([
                row["student_name"],
                row["status"],
                score,
                plag,
                appeal,
            ])

        col_widths = [80, 60, 50, 40, 60]
        table = Table(table_data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
        ]))
        elements.append(table)

        # 生成时间
        elements.append(Spacer(1, 10 * mm))
        elements.append(Paragraph(
            f"生成时间: {data['generated_at']}", cn_normal
        ))

        try:
            doc.build(elements)
            logger.info(f"PDF 报告已生成: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"PDF 生成失败: {e}, fallback到HTML")
            return None

    def _register_chinese_font(self) -> str:
        """注册中文字体，返回字体名"""
        # 尝试常见的中文字体路径
        font_paths = [
            # Windows
            "C:/Windows/Fonts/simhei.ttf",    # 黑体
            "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
            "C:/Windows/Fonts/simsun.ttc",     # 宋体
            # macOS
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            # Linux
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ]

        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    from reportlab.pdfbase import pdfmetrics
                    from reportlab.pdfbase.ttfonts import TTFont
                    name = os.path.splitext(os.path.basename(fp))[0]
                    pdfmetrics.registerFont(TTFont(name, fp))
                    logger.info(f"注册中文字体: {name} ({fp})")
                    return name
                except Exception:
                    continue

        # 无中文字体 → 使用默认
        logger.warning("未找到中文字体，PDF 可能无法正确显示中文")
        return "Helvetica"

    # ── HTML fallback ───────────────────────

    def _generate_html(self, data: Dict) -> str:
        """HTML 报告 fallback"""
        output_path = os.path.join(
            self.output_dir,
            f"report_{data['assignment_id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        )

        rows_html = ""
        for row in data["submissions"]:
            score = f"{row['score']:.1f}" if row['score'] is not None else "-"
            plag_icon = "⚠️" if row["plagiarized"] else ""
            appeal_badge = {
                "none": "", "pending": '<span style="color:orange">待处理</span>',
                "approved": '<span style="color:green">已批准</span>',
                "rejected": '<span style="color:red">已驳回</span>',
            }.get(row["appeal_status"], "")

            dim_html = ""
            if row.get("dimension_scores"):
                dim_parts = []
                for k, v in row["dimension_scores"].items():
                    bar_color = "#4caf50" if v >= 70 else "#ff9800" if v >= 50 else "#f44336"
                    dim_parts.append(
                        f'<span style="margin-right:10px">{k}: '
                        f'<b style="color:{bar_color}">{v:.1f}</b></span>'
                    )
                dim_html = "<br><small>" + " | ".join(dim_parts) + "</small>"

            rows_html += f"""
            <tr>
                <td>{row['student_name']}</td>
                <td>{row['status']}</td>
                <td><b>{score}</b></td>
                <td>{plag_icon}</td>
                <td>{appeal_badge}</td>
                <td style="text-align:left;font-size:0.85em">{row['feedback'][:80]}{dim_html}</td>
            </tr>"""

        dim_headers = ""
        if data["rubric_dimensions"]:
            dim_headers = "<p>评分维度: " + ", ".join(data["rubric_dimensions"]) + "</p>"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>批改报告 - {data['assignment_title']}</title>
<style>
body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 20px; }}
h1 {{ color: #333; border-bottom: 2px solid #4a90d9; padding-bottom: 10px; }}
.summary {{ background: #f5f5f5; padding: 15px; border-radius: 8px; margin: 15px 0; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 15px; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: center; }}
th {{ background: #4a90d9; color: white; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
</style>
</head>
<body>
<h1>📊 批改报告 - {data['assignment_title']}</h1>
<div class="summary">
<p>提交数: <b>{data['total_submissions']}</b> | 平均分: <b>{data['avg_score']}</b>
 | 最高分: <b>{data['max_score']}</b> | 最低分: <b>{data['min_score']}</b></p>
{dim_headers}
<p>生成时间: {data['generated_at']}</p>
</div>
<table>
<tr><th>学生</th><th>状态</th><th>分数</th><th>抄袭</th><th>申诉</th><th>反馈</th></tr>
{rows_html}
</table>
</body>
</html>"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"HTML 报告已生成: {output_path}")
        return output_path

    # ── 缓存 ────────────────────────────────

    def _cache_key(self, assignment_id: str) -> str:
        """生成缓存键"""
        return hashlib.md5(assignment_id.encode()).hexdigest()

    def _find_cached_report(self, assignment_id: str) -> Optional[str]:
        """在磁盘上查找缓存的报告"""
        prefix = f"report_{assignment_id}_"
        if not os.path.exists(self.output_dir):
            return None
        for fname in os.listdir(self.output_dir):
            if fname.startswith(prefix) and (fname.endswith(".pdf") or fname.endswith(".html")):
                return os.path.join(self.output_dir, fname)
        return None
