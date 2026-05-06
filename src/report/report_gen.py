"""
报告生成器（M12 增强）
支持：雷达图 + 课件依据 + 学生排名 + 改进建议
输出格式：reportlab PDF（含 matplotlib 雷达图） / HTML fallback（含内联 SVG 雷达图）
"""
from __future__ import annotations
import os
import io
import json
import hashlib
import logging
import math
import uuid
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from storage.db import DB
from contracts.models import (
    ReportConfig, ReportEvidence, RadarData,
    ReportType, RubricDimension
)

logger = logging.getLogger("report")


class ReportGenerator:
    """
    批改报告生成器（M12 增强）
    - reportlab PDF 输出（含中文字体 + matplotlib 雷达图）
    - HTML fallback（含内联 SVG 雷达图）
    - 课件依据引用 + 改进建议
    - 学生排名 + 维度对比
    - 惰性缓存 + 失效机制
    """

    def __init__(self, db: DB, output_dir: str = "reports",
                 knowledge_base=None):
        self.db = db
        self.output_dir = output_dir
        self.knowledge_base = knowledge_base
        self._cache: Dict[str, str] = {}  # cache_key → file_path
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    # ── 对外接口 ──────────────────────────────

    def get_or_generate(self, assignment_id: str,
                        force: bool = False) -> str:
        """惰性获取报告（带缓存），兼容旧接口"""
        config = ReportConfig(assignment_id=assignment_id)
        return self.generate_with_config(config, force=force)

    def generate_with_config(self, config: ReportConfig,
                             force: bool = False) -> str:
        """
        根据配置生成报告（M12 增强入口）
        Returns: 报告文件路径
        """
        cache_key = self._cache_key(
            config.assignment_id,
            config.report_type,
            config.student_id
        )

        if not force and cache_key in self._cache:
            path = self._cache[cache_key]
            if os.path.exists(path):
                logger.info(f"报告缓存命中: {cache_key}")
                return path

        # 检查磁盘缓存
        if not force:
            disk_path = self._find_cached_report(
                config.assignment_id, config.report_type, config.student_id
            )
            if disk_path:
                self._cache[cache_key] = disk_path
                return disk_path

        # 生成报告
        path = self._generate_report(config)
        self._cache[cache_key] = path
        return path

    def generate_student_report(self, assignment_id: str,
                                student_id: str,
                                force: bool = False) -> str:
        """生成学生个人报告（含雷达图+课件依据）"""
        config = ReportConfig(
            assignment_id=assignment_id,
            report_type=ReportType.STUDENT_DETAIL,
            student_id=student_id,
            include_radar=True,
            include_evidence=True,
        )
        return self.generate_with_config(config, force=force)

    def generate_radar_comparison(self, assignment_id: str,
                                  student_ids: List[str],
                                  force: bool = False) -> str:
        """生成多学生雷达图对比报告"""
        config = ReportConfig(
            assignment_id=assignment_id,
            report_type=ReportType.RADAR_COMPARISON,
            comparison_students=student_ids,
            include_radar=True,
            include_evidence=False,
        )
        return self.generate_with_config(config, force=force)

    def invalidate(self, assignment_id: str):
        """失效缓存"""
        keys_to_remove = [
            k for k in self._cache
            if k.startswith(self._cache_key_prefix(assignment_id))
        ]
        for k in keys_to_remove:
            del self._cache[k]

    # ── 数据收集 ────────────────────────────

    def _collect_data(self, assignment_id: str) -> Dict:
        """收集报告数据（增强版：含维度统计+排名）"""
        assignment = self.db.get_assignment(assignment_id)
        submissions = self.db.list_submissions(assignment_id)

        # 按分数排序生成排名
        scored = sorted(
            [s for s in submissions if s.score is not None],
            key=lambda s: s.score, reverse=True
        )

        rows = []
        for rank_idx, s in enumerate(scored):
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
                "rank": rank_idx + 1,
            }
            if result:
                row["dimension_scores"] = result.dimension_scores
                row["confidence"] = result.confidence
            rows.append(row)

        # 也加入未评分的
        for s in submissions:
            if s.score is None:
                result = self.db.get_grading_result(s.id)
                row = {
                    "student_id": s.student_id,
                    "student_name": s.student_name,
                    "status": s.status,
                    "score": None,
                    "feedback": s.feedback or "",
                    "plagiarized": s.plagiarized,
                    "appeal_status": s.appeal_status,
                    "submitted_at": s.submitted_at.isoformat(),
                    "rank": None,
                }
                if result:
                    row["dimension_scores"] = result.dimension_scores
                    row["confidence"] = result.confidence
                rows.append(row)

        scores = [s.score for s in submissions if s.score is not None]
        rubric = None
        if assignment and assignment.rubric_id:
            rubric = self.db.get_rubric(assignment.rubric_id)

        # 维度统计
        dim_stats = self.db.get_dimension_stats(assignment_id)

        return {
            "assignment_id": assignment_id,
            "assignment_title": assignment.title if assignment else assignment_id,
            "generated_at": datetime.now().isoformat(),
            "total_submissions": len(submissions),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "max_score": max(scores) if scores else 0,
            "min_score": min(scores) if scores else 0,
            "median_score": self._median(scores) if scores else 0,
            "rubric_dimensions": (
                [d.name for d in rubric.dimensions]
                if rubric else []
            ),
            "rubric_dimension_details": (
                [{"name": d.name, "weight": d.weight,
                  "max_score": d.max_score, "description": d.description}
                 for d in rubric.dimensions]
                if rubric else []
            ),
            "dim_stats": dim_stats,
            "submissions": rows,
        }

    def _collect_radar_data(self, assignment_id: str,
                            student_ids: List[str] = None) -> RadarData:
        """收集雷达图数据"""
        assignment = self.db.get_assignment(assignment_id)
        if not assignment or not assignment.rubric_id:
            return RadarData(dimensions=[], class_avg=[])

        rubric = self.db.get_rubric(assignment.rubric_id)
        if not rubric:
            return RadarData(dimensions=[], class_avg=[])

        dim_names = [d.name for d in rubric.dimensions]
        dim_max = [d.max_score for d in rubric.dimensions]

        # 班级平均
        dim_stats = self.db.get_dimension_stats(assignment_id)
        class_avg = [dim_stats.get(d, {}).get("avg", 0) for d in dim_names]

        # 学生各维度分数
        student_scores: Dict[str, List[float]] = {}
        target_ids = student_ids or []
        if target_ids:
            for sid in target_ids:
                sub = self.db.get_student_submission(assignment_id, sid)
                if sub:
                    result = self.db.get_grading_result(sub.id)
                    if result and result.dimension_scores:
                        student_scores[sid] = [
                            result.dimension_scores.get(d, 0)
                            for d in dim_names
                        ]

        return RadarData(
            dimensions=dim_names,
            class_avg=class_avg,
            student_scores=student_scores,
            max_scores=dim_max,
        )

    def _collect_evidences(self, assignment_id: str,
                           radar_data: RadarData) -> List[ReportEvidence]:
        """收集课件依据"""
        assignment = self.db.get_assignment(assignment_id)
        course_id = assignment.course_id if assignment else "default"
        evidences = []
        for i, dim_name in enumerate(radar_data.dimensions):
            avg = radar_data.class_avg[i] if i < len(radar_data.class_avg) else 0
            max_s = radar_data.max_scores[i] if i < len(radar_data.max_scores) else 100
            ratio = avg / max_s if max_s > 0 else 0

            # 薄弱程度
            if ratio < 0.5:
                level = "weak"
            elif ratio < 0.7:
                level = "medium"
            else:
                level = "strong"

            # 从知识库检索相关课件段落
            refs = []
            if self.knowledge_base:
                try:
                    results = self.knowledge_base.search_results(
                        dim_name,
                        top_k=3,
                        course_id=course_id,
                    )
                    refs = [item.as_reference_text() for item in results[:3]]
                except Exception as e:
                    logger.warning(f"知识库检索失败: {dim_name}, {e}")

            # 改进建议
            tips = self._generate_tips(dim_name, level, ratio)

            evidences.append(ReportEvidence(
                dimension_name=dim_name,
                avg_score=avg,
                max_score=max_s,
                weakness_level=level,
                courseware_refs=refs,
                improvement_tips=tips,
            ))

        return evidences

    def _generate_tips(self, dim_name: str, level: str,
                       ratio: float) -> List[str]:
        """根据薄弱程度生成改进建议"""
        tips = []
        if level == "weak":
            tips.append(f"「{dim_name}」维度得分偏低（达标率{ratio:.0%}），建议重点复习相关课件章节")
            tips.append(f"建议布置针对性练习，巩固{dim_name}知识点")
        elif level == "medium":
            tips.append(f"「{dim_name}」维度表现中等（达标率{ratio:.0%}），仍有提升空间")
            tips.append(f"可通过课后练习加强对{dim_name}的理解")
        else:
            tips.append(f"「{dim_name}」维度表现良好（达标率{ratio:.0%}），继续保持")
        return tips

    @staticmethod
    def _median(values: List[float]) -> float:
        """计算中位数"""
        if not values:
            return 0
        sorted_v = sorted(values)
        n = len(sorted_v)
        if n % 2 == 1:
            return sorted_v[n // 2]
        return (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2

    # ── 报告生成核心 ─────────────────────────

    def _generate_report(self, config: ReportConfig) -> str:
        """根据配置生成报告"""
        data = self._collect_data(config.assignment_id)

        # 雷达图数据
        radar_data = None
        radar_img_path = None
        if config.include_radar and data["rubric_dimensions"]:
            student_ids = config.comparison_students
            if config.student_id:
                student_ids = [config.student_id]
            radar_data = self._collect_radar_data(
                config.assignment_id, student_ids
            )
            # 生成雷达图图片
            radar_img_path = self._generate_radar_image(radar_data)

        # 课件依据
        evidences = None
        if config.include_evidence and radar_data and radar_data.dimensions:
            evidences = self._collect_evidences(
                config.assignment_id, radar_data
            )

        # 学生筛选（个人报告）
        if config.student_id:
            data["submissions"] = [
                s for s in data["submissions"]
                if s.get("student_id") == config.student_id
            ]

        data["config"] = config
        data["radar_data"] = radar_data
        data["radar_img_path"] = radar_img_path
        data["evidences"] = evidences

        # 尝试 PDF
        path = self._generate_pdf(data)
        if path:
            # 保存报告记录到 DB
            self._save_report_record(config, path, radar_data, evidences)
            return path

        # fallback: HTML
        path = self._generate_html(data)
        self._save_report_record(config, path, radar_data, evidences)
        return path

    def _save_report_record(self, config: ReportConfig, path: str,
                            radar_data: Optional[RadarData],
                            evidences: Optional[List[ReportEvidence]]):
        """保存报告记录到 DB"""
        report_id = f"rpt_{uuid.uuid4().hex[:8]}"
        try:
            self.db.save_report(
                report_id=report_id,
                assignment_id=config.assignment_id,
                report_type=config.report_type,
                file_path=path,
                config=config,
                student_id=config.student_id,
                radar_included=radar_data is not None and bool(radar_data.dimensions),
                evidence_included=evidences is not None and len(evidences) > 0,
            )
            # 保存课件依据
            if evidences:
                for ev in evidences:
                    self.db.save_report_evidence(report_id, ev)
            logger.info(f"报告记录已保存: {report_id}")
        except Exception as e:
            logger.warning(f"保存报告记录失败: {e}")

    # ── 雷达图生成 ─────────────────────────────

    def _generate_radar_image(self, radar_data: RadarData) -> Optional[str]:
        """生成雷达图 PNG 图片（matplotlib）"""
        if not radar_data.dimensions:
            return None

        try:
            import matplotlib
            matplotlib.use('Agg')  # 非交互式后端
            import matplotlib.pyplot as plt
            from matplotlib.font_manager import FontProperties
        except ImportError:
            logger.warning("matplotlib 未安装，跳过雷达图图片生成")
            return None

        # 中文字体
        plt.rcParams['font.sans-serif'] = [
            'SimHei', 'Microsoft YaHei', 'PingFang SC',
            'WenQuanYi Micro Hei', 'DejaVu Sans'
        ]
        plt.rcParams['axes.unicode_minus'] = False

        n = len(radar_data.dimensions)
        if n < 3:
            logger.warning("雷达图需要至少3个维度")
            return None

        # 角度计算
        angles = [i / n * 2 * math.pi for i in range(n)]
        angles += angles[:1]  # 闭合

        fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))

        # 满分参考线
        if radar_data.max_scores:
            max_vals = radar_data.max_scores + radar_data.max_scores[:1]
            ax.plot(angles, max_vals, 'k--', linewidth=1, alpha=0.3, label='满分')
            ax.fill(angles, max_vals, alpha=0.05, color='gray')

        # 班级平均
        avg_vals = radar_data.class_avg + radar_data.class_avg[:1]
        ax.plot(angles, avg_vals, 'b-', linewidth=2, label='班级平均')
        ax.fill(angles, avg_vals, alpha=0.15, color='blue')

        # 学生分数
        colors = ['#e74c3c', '#2ecc71', '#f39c12', '#9b59b6', '#1abc9c']
        for idx, (sid, scores) in enumerate(radar_data.student_scores.items()):
            vals = scores + scores[:1]
            color = colors[idx % len(colors)]
            label = f"学生 {sid[:6]}"
            ax.plot(angles, vals, '-', linewidth=2, color=color, label=label)
            ax.fill(angles, vals, alpha=0.1, color=color)

        # 维度标签
        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(radar_data.dimensions, fontsize=11)

        # 标题和图例
        ax.set_title('评分维度雷达图', fontsize=16, pad=20)
        ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1))

        # 保存
        output_path = os.path.join(
            self.output_dir,
            f"radar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        fig.savefig(output_path, dpi=150, bbox_inches='tight',
                    facecolor='white', edgecolor='none')
        plt.close(fig)
        logger.info(f"雷达图已生成: {output_path}")
        return output_path

    def _generate_radar_svg(self, radar_data: RadarData) -> str:
        """生成雷达图内联 SVG（用于 HTML 报告，无需 matplotlib）"""
        if not radar_data.dimensions:
            return ""

        n = len(radar_data.dimensions)
        if n < 3:
            return ""

        # SVG 画布参数
        size = 400
        cx, cy = size / 2, size / 2
        radius = 150

        # 角度
        angles = [i / n * 2 * math.pi - math.pi / 2 for i in range(n)]

        # 计算最大值（用于归一化）
        max_val = max(radar_data.max_scores) if radar_data.max_scores else 100

        def to_point(idx, val):
            r = (val / max_val) * radius if max_val > 0 else 0
            x = cx + r * math.cos(angles[idx])
            y = cy + r * math.sin(angles[idx])
            return f"{x:.1f},{y:.1f}"

        # 网格环（5层）
        grid_rings = ""
        for level in range(1, 6):
            r = (level / 5) * radius
            points = " ".join([
                f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}"
                for a in angles
            ])
            grid_rings += f'<polygon points="{points}" fill="none" stroke="#ddd" stroke-width="0.5"/>\n'

        # 轴线
        axis_lines = ""
        for a in angles:
            x = cx + radius * math.cos(a)
            y = cy + radius * math.sin(a)
            axis_lines += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#ccc" stroke-width="0.5"/>\n'

        # 维度标签
        labels = ""
        for i, dim in enumerate(radar_data.dimensions):
            lx = cx + (radius + 25) * math.cos(angles[i])
            ly = cy + (radius + 25) * math.sin(angles[i])
            anchor = "middle"
            if math.cos(angles[i]) > 0.3:
                anchor = "start"
            elif math.cos(angles[i]) < -0.3:
                anchor = "end"
            labels += f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" font-size="11" fill="#333">{dim}</text>\n'

        # 班级平均多边形
        avg_points = " ".join([
            to_point(i, v) for i, v in enumerate(radar_data.class_avg)
        ])
        avg_polygon = (
            f'<polygon points="{avg_points}" fill="rgba(66,133,244,0.15)" '
            f'stroke="#4285f4" stroke-width="2"/>'
        )

        # 学生多边形
        student_polygons = ""
        colors = ["#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c"]
        for idx, (sid, scores) in enumerate(radar_data.student_scores.items()):
            pts = " ".join([
                to_point(i, v) for i, v in enumerate(scores)
            ])
            c = colors[idx % len(colors)]
            student_polygons += (
                f'<polygon points="{pts}" fill="{c}22" '
                f'stroke="{c}" stroke-width="2" stroke-dasharray="5,3"/>'
            )

        # 图例
        legend = f'<circle cx="30" cy="{size - 40}" r="5" fill="#4285f4"/>'
        legend += f'<text x="40" y="{size - 36}" font-size="10" fill="#333">班级平均</text>'
        for idx, (sid, _) in enumerate(radar_data.student_scores.items()):
            c = colors[idx % len(colors)]
            y_pos = size - 25 + idx * 15
            legend += f'<circle cx="30" cy="{y_pos}" r="5" fill="{c}"/>'
            legend += f'<text x="40" y="{y_pos + 4}" font-size="10" fill="#333">学生 {sid[:6]}</text>'

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size + 30}" width="{size}">
        {grid_rings}
        {axis_lines}
        {avg_polygon}
        {student_polygons}
        {labels}
        {legend}
        <text x="{cx}" y="20" text-anchor="middle" font-size="14" font-weight="bold" fill="#333">评分维度雷达图</text>
        </svg>"""

        return svg

    # ── PDF 生成 (reportlab) ────────────────

    def _generate_pdf(self, data: Dict) -> Optional[str]:
        """使用 reportlab 生成 PDF"""
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                Image, PageBreak
            )
            from reportlab.lib import colors
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
        except ImportError:
            logger.warning("reportlab 未安装, fallback到HTML")
            return None

        # 注册中文字体
        font_name = self._register_chinese_font()

        config: ReportConfig = data.get("config", ReportConfig(
            assignment_id=data["assignment_id"]
        ))

        suffix = ""
        if config.student_id:
            suffix = f"_student_{config.student_id}"
        elif config.report_type == ReportType.RADAR_COMPARISON:
            suffix = "_radar_comparison"

        output_path = os.path.join(
            self.output_dir,
            f"report_{data['assignment_id']}{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
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
        cn_small = ParagraphStyle(
            "CNSmall", parent=styles["Normal"],
            fontName=font_name, fontSize=9, leading=12,
        )

        elements = []

        # 标题
        report_title = "批改报告"
        if config.report_type == ReportType.STUDENT_DETAIL:
            report_title = "学生个人报告"
        elif config.report_type == ReportType.RADAR_COMPARISON:
            report_title = "雷达图对比报告"

        elements.append(Paragraph(
            f"{report_title} - {data['assignment_title']}", cn_title
        ))
        elements.append(Spacer(1, 10 * mm))

        # 概要
        summary_text = (
            f"提交数: {data['total_submissions']} | "
            f"平均分: {data['avg_score']} | "
            f"中位数: {data.get('median_score', 0)} | "
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

        # ── 雷达图 ──
        if data.get("radar_img_path") and os.path.exists(data["radar_img_path"]):
            elements.append(Paragraph("评分维度雷达图", cn_heading))
            elements.append(Spacer(1, 3 * mm))
            try:
                img = Image(data["radar_img_path"], width=400, height=400)
                elements.append(img)
                elements.append(Spacer(1, 5 * mm))
            except Exception as e:
                logger.warning(f"雷达图嵌入PDF失败: {e}")

        # ── 维度统计表 ──
        if data.get("dim_stats"):
            elements.append(Paragraph("维度统计", cn_heading))
            elements.append(Spacer(1, 3 * mm))
            dim_headers = ["维度", "平均分", "最高分", "最低分", "人数"]
            dim_table_data = [dim_headers]
            for dim_name, stats in data["dim_stats"].items():
                dim_table_data.append([
                    dim_name,
                    f"{stats['avg']:.1f}",
                    f"{stats['max']:.1f}",
                    f"{stats['min']:.1f}",
                    str(stats["count"]),
                ])
            dim_table = Table(dim_table_data, colWidths=[100, 60, 60, 60, 50])
            dim_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4a90d9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
            ]))
            elements.append(dim_table)
            elements.append(Spacer(1, 5 * mm))

        # ── 课件依据 ──
        evidences = data.get("evidences")
        if evidences:
            elements.append(PageBreak())
            elements.append(Paragraph("课件依据与改进建议", cn_heading))
            elements.append(Spacer(1, 3 * mm))

            for ev in evidences:
                level_icon = {
                    "weak": "🔴", "medium": "🟡", "strong": "🟢"
                }.get(ev.weakness_level, "⚪")
                level_text = {
                    "weak": "薄弱", "medium": "中等", "strong": "良好"
                }.get(ev.weakness_level, "未知")

                elements.append(Paragraph(
                    f"{level_icon} {ev.dimension_name} "
                    f"(平均 {ev.avg_score:.1f}/{ev.max_score:.0f}, {level_text})",
                    cn_heading
                ))
                elements.append(Spacer(1, 2 * mm))

                if ev.courseware_refs:
                    elements.append(Paragraph("课件依据:", cn_small))
                    for ref in ev.courseware_refs:
                        elements.append(Paragraph(
                            f"  · {ref[:100]}", cn_small
                        ))

                if ev.improvement_tips:
                    elements.append(Paragraph("改进建议:", cn_small))
                    for tip in ev.improvement_tips:
                        elements.append(Paragraph(
                            f"  → {tip}", cn_small
                        ))

                elements.append(Spacer(1, 3 * mm))

        # ── 学生成绩表 ──
        elements.append(PageBreak())
        elements.append(Paragraph("学生成绩明细", cn_heading))
        elements.append(Spacer(1, 3 * mm))

        # 表头
        headers = ["排名", "学生", "状态", "分数", "抄袭", "申诉"]
        table_data = [headers]

        for row in data["submissions"]:
            score = f"{row['score']:.1f}" if row['score'] is not None else "-"
            plag = "⚠️" if row["plagiarized"] else ""
            appeal = {
                "none": "", "pending": "待处理",
                "approved": "已批准", "rejected": "已驳回"
            }.get(row["appeal_status"], "")
            rank = str(row["rank"]) if row.get("rank") else "-"
            table_data.append([
                rank,
                row["student_name"],
                row["status"],
                score,
                plag,
                appeal,
            ])

        col_widths = [40, 80, 60, 50, 40, 60]
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
        """HTML 报告（含内联 SVG 雷达图 + 课件依据）"""
        config: ReportConfig = data.get("config", ReportConfig(
            assignment_id=data["assignment_id"]
        ))

        suffix = ""
        if config.student_id:
            suffix = f"_student_{config.student_id}"

        output_path = os.path.join(
            self.output_dir,
            f"report_{data['assignment_id']}{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        )

        # ── 学生成绩表行 ──
        rows_html = ""
        for row in data["submissions"]:
            score = f"{row['score']:.1f}" if row['score'] is not None else "-"
            plag_icon = "⚠️" if row["plagiarized"] else ""
            appeal_badge = {
                "none": "", "pending": '<span style="color:orange">待处理</span>',
                "approved": '<span style="color:green">已批准</span>',
                "rejected": '<span style="color:red">已驳回</span>',
            }.get(row["appeal_status"], "")
            rank = str(row.get("rank", "-"))

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
                <td>{rank}</td>
                <td>{row['student_name']}</td>
                <td>{row['status']}</td>
                <td><b>{score}</b></td>
                <td>{plag_icon}</td>
                <td>{appeal_badge}</td>
                <td style="text-align:left;font-size:0.85em">{row['feedback'][:80]}{dim_html}</td>
            </tr>"""

        # ── 维度统计表 ──
        dim_stats_html = ""
        if data.get("dim_stats"):
            dim_stats_html = '<h2>维度统计</h2><table><tr><th>维度</th><th>平均分</th><th>最高分</th><th>最低分</th><th>人数</th></tr>'
            for dim_name, stats in data["dim_stats"].items():
                avg_color = "#4caf50" if stats["avg"] >= 70 else "#ff9800" if stats["avg"] >= 50 else "#f44336"
                dim_stats_html += f'<tr><td>{dim_name}</td><td style="color:{avg_color};font-weight:bold">{stats["avg"]:.1f}</td><td>{stats["max"]:.1f}</td><td>{stats["min"]:.1f}</td><td>{stats["count"]}</td></tr>'
            dim_stats_html += '</table>'

        # ── 雷达图 SVG ──
        radar_html = ""
        radar_data: Optional[RadarData] = data.get("radar_data")
        if radar_data and radar_data.dimensions:
            svg = self._generate_radar_svg(radar_data)
            if svg:
                radar_html = f'<h2>评分维度雷达图</h2><div style="text-align:center">{svg}</div>'

        # ── 课件依据 ──
        evidence_html = ""
        evidences = data.get("evidences")
        if evidences:
            evidence_html = '<h2>课件依据与改进建议</h2>'
            for ev in evidences:
                level_icon = {"weak": "🔴", "medium": "🟡", "strong": "🟢"}.get(ev.weakness_level, "⚪")
                level_text = {"weak": "薄弱", "medium": "中等", "strong": "良好"}.get(ev.weakness_level, "未知")
                bg_color = {"weak": "#fff3f3", "medium": "#fffbe6", "strong": "#f0fff0"}.get(ev.weakness_level, "#f5f5f5")

                refs_html = ""
                if ev.courseware_refs:
                    refs_items = "".join([f'<li style="font-size:0.9em">{ref[:150]}</li>' for ref in ev.courseware_refs])
                    refs_html = f'<p style="margin:5px 0"><b>课件依据:</b></p><ul style="margin:2px 0 2px 15px">{refs_items}</ul>'

                tips_html = ""
                if ev.improvement_tips:
                    tips_items = "".join([f'<li style="font-size:0.9em">{tip}</li>' for tip in ev.improvement_tips])
                    tips_html = f'<p style="margin:5px 0"><b>改进建议:</b></p><ul style="margin:2px 0 2px 15px">{tips_items}</ul>'

                evidence_html += f'''
                <div style="background:{bg_color};padding:12px;border-radius:8px;margin:10px 0;border-left:4px solid {
                    "#e74c3c" if ev.weakness_level == "weak" else "#f39c12" if ev.weakness_level == "medium" else "#27ae60"
                }">
                    <p style="margin:0"><b>{level_icon} {ev.dimension_name}</b> — 平均 {ev.avg_score:.1f}/{ev.max_score:.0f} ({level_text})</p>
                    {refs_html}
                    {tips_html}
                </div>'''

        dim_headers = ""
        if data["rubric_dimensions"]:
            dim_headers = "<p>评分维度: " + ", ".join(data["rubric_dimensions"]) + "</p>"

        # 报告类型标题
        report_title = "📊 批改报告"
        if config.report_type == ReportType.STUDENT_DETAIL:
            report_title = "👤 学生个人报告"
        elif config.report_type == ReportType.RADAR_COMPARISON:
            report_title = "📈 雷达图对比报告"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{report_title} - {data['assignment_title']}</title>
<style>
body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 20px; max-width: 1100px; }}
h1 {{ color: #333; border-bottom: 2px solid #4a90d9; padding-bottom: 10px; }}
h2 {{ color: #4a90d9; border-bottom: 1px solid #ddd; padding-bottom: 5px; margin-top: 30px; }}
.summary {{ background: #f5f5f5; padding: 15px; border-radius: 8px; margin: 15px 0; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 15px; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: center; }}
th {{ background: #4a90d9; color: white; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
.evidence-section {{ margin-top: 30px; }}
</style>
</head>
<body>
<h1>{report_title} - {data['assignment_title']}</h1>
<div class="summary">
<p>提交数: <b>{data['total_submissions']}</b> | 平均分: <b>{data['avg_score']}</b>
 | 中位数: <b>{data.get('median_score', 0)}</b>
 | 最高分: <b>{data['max_score']}</b> | 最低分: <b>{data['min_score']}</b></p>
{dim_headers}
<p>生成时间: {data['generated_at']}</p>
</div>

{radar_html}

{dim_stats_html}

{evidence_html}

<h2>学生成绩明细</h2>
<table>
<tr><th>排名</th><th>学生</th><th>状态</th><th>分数</th><th>抄袭</th><th>申诉</th><th>反馈</th></tr>
{rows_html}
</table>
</body>
</html>"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"HTML 报告已生成: {output_path}")
        return output_path

    # ── 缓存 ────────────────────────────────

    def _cache_key(self, assignment_id: str,
                   report_type: str = "",
                   student_id: str = None) -> str:
        """生成缓存键"""
        raw = f"{assignment_id}:{report_type}:{student_id or ''}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _cache_key_prefix(self, assignment_id: str) -> str:
        """缓存键前缀（用于 invalidate）"""
        return hashlib.md5(f"{assignment_id}:".encode()).hexdigest()[:8]

    def _find_cached_report(self, assignment_id: str,
                            report_type: str = "",
                            student_id: str = None) -> Optional[str]:
        """在磁盘上查找缓存的报告"""
        if not os.path.exists(self.output_dir):
            return None

        suffix = ""
        if student_id:
            suffix = f"_student_{student_id}"
        elif report_type == ReportType.RADAR_COMPARISON:
            suffix = "_radar_comparison"

        prefix = f"report_{assignment_id}{suffix}_"
        for fname in os.listdir(self.output_dir):
            if fname.startswith(prefix) and (fname.endswith(".pdf") or fname.endswith(".html")):
                return os.path.join(self.output_dir, fname)
        return None
