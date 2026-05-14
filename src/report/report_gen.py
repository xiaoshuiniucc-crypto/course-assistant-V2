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
from grading.evidence import build_grading_evidence
from runtime.localization import (
    localize_dimension_name,
    localize_user_text,
    translate_appeal_status,
    translate_confidence_label,
    translate_submission_status,
)

logger = logging.getLogger("report")


def _patch_hashlib_usedforsecurity_compat() -> None:
    """Make hashlib.md5 tolerate usedforsecurity kwarg on incompatible builds."""
    md5_func = getattr(hashlib, "md5", None)
    if not callable(md5_func) or getattr(md5_func, "_course_assistant_patched", False):
        return

    def _wrapped_md5(*args, **kwargs):
        kwargs.pop("usedforsecurity", None)
        return md5_func(*args, **kwargs)

    _wrapped_md5._course_assistant_patched = True  # type: ignore[attr-defined]
    hashlib.md5 = _wrapped_md5


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
        rubric = None
        if assignment and assignment.rubric_id:
            rubric = self.db.get_rubric(assignment.rubric_id)

        # 按分数排序生成排名
        scored = sorted(
            [s for s in submissions if s.score is not None],
            key=lambda s: s.score, reverse=True
        )

        rows = []

        def build_row(s, rank):
            result = self.db.get_grading_result(s.id)
            row = {
                "student_id": s.student_id,
                "student_name": s.student_name,
                "status": s.status,
                "status_display": translate_submission_status(s.status),
                "score": s.score,
                "feedback": localize_user_text(s.feedback or ""),
                "plagiarized": s.plagiarized,
                "appeal_status": s.appeal_status,
                "submitted_at": s.submitted_at.isoformat(),
                "rank": rank,
            }
            if result:
                row["dimension_scores"] = result.dimension_scores
                row["dimension_scores_display"] = {
                    localize_dimension_name(name): value
                    for name, value in result.dimension_scores.items()
                }
                row["confidence"] = result.confidence
                row["confidence_label"] = result.confidence_label
                row["confidence_label_display"] = translate_confidence_label(
                    result.confidence_label
                )
                row["gain_points"] = result.gain_points
                row["deductions"] = result.deductions
                row["regrade_diff"] = result.regrade_diff
                row["grading_context"] = result.grading_context
                row["grading_evidence"] = [
                    localize_user_text(item)
                    for item in build_grading_evidence(result, rubric)
                ]
            return row

        for rank_idx, s in enumerate(scored):
            rows.append(build_row(s, rank_idx + 1))

        # 也加入未评分的
        for s in submissions:
            if s.score is None:
                rows.append(build_row(s, None))

        scores = [s.score for s in submissions if s.score is not None]

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
                [localize_dimension_name(d.name) for d in rubric.dimensions]
                if rubric else []
            ),
            "rubric_dimension_details": (
                [{"name": localize_dimension_name(d.name), "weight": d.weight,
                  "max_score": d.max_score, "description": localize_user_text(d.description)}
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
            localized_dim_name = localize_dimension_name(dim_name)
            tips = self._generate_tips(localized_dim_name, level, ratio)

            evidences.append(ReportEvidence(
                dimension_name=localized_dim_name,
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
    def _format_structured_items(items: List[str], empty_text: str) -> str:
        if not items:
            return f"<span style='color:#777'>{empty_text}</span>"
        return "".join(
            f"<li style='font-size:0.9em'>{item}</li>"
            for item in items
        )

    def _build_row_detail_html(self, row: Dict) -> str:
        confidence_html = ""
        if row.get("confidence") is not None:
            label = row.get("confidence_label_display") or translate_confidence_label(
                row.get("confidence_label") or "medium"
            )
            confidence_html = (
                f"<p style='margin:6px 0 0 0;color:#555'>"
                f"AI置信度: <b>{label}</b> ({row['confidence']:.2f})"
                f"</p>"
            )

        gain_items: List[str] = []
        for dim_name, points in (row.get("gain_points") or {}).items():
            if points:
                gain_items.append(
                    f"{localize_dimension_name(dim_name)}: "
                    f"{'；'.join(localize_user_text(point) for point in points[:2])}"
                )

        deduction_items: List[str] = []
        for dim_name, items in (row.get("deductions") or {}).items():
            for item in items[:2]:
                point = localize_user_text(str(item.get("point", "")).strip()) or "存在扣分点"
                deduct = item.get("deduct", 0)
                evidence = localize_user_text(str(item.get("evidence", "")).strip())
                source = localize_user_text(str(item.get("evidence_source", "")).strip())
                detail = f"{localize_dimension_name(dim_name)}: {point}"
                if deduct:
                    detail += f" (-{deduct})"
                if source:
                    detail += f"；来源: {source}"
                if evidence:
                    detail += f"；依据: {evidence[:80]}"
                deduction_items.append(detail)

        diff_items: List[str] = []
        for dim_name, item in (row.get("regrade_diff") or {}).items():
            if not isinstance(item, dict):
                continue
            old_score = item.get("old")
            new_score = item.get("new")
            reason = localize_user_text(str(item.get("reason", "")).strip())
            diff_line = f"{localize_dimension_name(dim_name)}: {old_score} -> {new_score}"
            if reason:
                diff_line += f"；原因: {reason}"
            diff_items.append(diff_line)

        sections = [
            "<div style='margin-top:8px;padding-top:8px;border-top:1px dashed #d8e5f2'>",
            confidence_html,
            "<p style='margin:8px 0 4px 0'><b>得分点</b></p>",
            (
                "<ul style='margin:2px 0 8px 18px'>"
                f"{self._format_structured_items(gain_items, '暂无结构化得分点')}"
                "</ul>"
            ),
            "<p style='margin:8px 0 4px 0'><b>扣分依据</b></p>",
            (
                "<ul style='margin:2px 0 8px 18px'>"
                f"{self._format_structured_items(deduction_items, '暂无结构化扣分依据')}"
                "</ul>"
            ),
        ]

        if diff_items:
            sections.extend([
                "<p style='margin:8px 0 4px 0'><b>重评分差异</b></p>",
                (
                    "<ul style='margin:2px 0 8px 18px'>"
                    f"{self._format_structured_items(diff_items, '暂无重评分差异')}"
                    "</ul>"
                ),
            ])

        sections.append("</div>")
        return "".join(sections)

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

    @staticmethod
    def _wrap_chart_label(label: str, max_chars: int = 8) -> List[str]:
        text = (label or '').strip()
        if not text:
            return ['']
        compact = (
            text.replace("（", " ")
            .replace("）", " ")
            .replace("(", " ")
            .replace(")", " ")
            .replace("：", " ")
            .replace(":", " ")
        )
        compact = compact.replace('/', ' / ').replace('|', ' | ')
        tokens = [tok for tok in compact.split() if tok]
        if len(tokens) >= 2 and all(len(tok) <= max_chars for tok in tokens):
            return tokens[:3]
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    @classmethod
    def _short_chart_label(cls, label: str, max_chars: int = 10, max_lines: int = 3) -> str:
        parts = cls._wrap_chart_label(label, max_chars=max_chars)
        if len(parts) > max_lines:
            parts = parts[:max_lines]
            parts[-1] = parts[-1].rstrip("。；，、 ") + "…"
        return "\n".join(parts)

    @classmethod
    def _svg_chart_label(cls, label: str, max_chars: int = 8, max_lines: int = 3) -> str:
        parts = cls._wrap_chart_label(label, max_chars=max_chars)
        if len(parts) > max_lines:
            parts = parts[:max_lines]
            parts[-1] = parts[-1].rstrip("。；，、 ") + "…"
        return '<br/>'.join(parts)


    def _generate_radar_image(self, radar_data: RadarData) -> Optional[str]:
        """生成雷达图 PNG，优先用于 matplotlib 渲染。"""
        if not radar_data.dimensions:
            return None

        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError:
            logger.warning("matplotlib 不可用，无法生成 PNG 雷达图")
            return None

        plt.rcParams['font.sans-serif'] = [
            'SimHei', 'Microsoft YaHei', 'PingFang SC',
            'WenQuanYi Micro Hei', 'DejaVu Sans'
        ]
        plt.rcParams['axes.unicode_minus'] = False

        n = len(radar_data.dimensions)
        if n < 3:
            logger.warning("雷达图至少需要 3 个维度")
            return None

        angles = [i / n * 2 * math.pi for i in range(n)]
        angles += angles[:1]
        labels = [self._short_chart_label(dim, max_chars=6, max_lines=3) for dim in radar_data.dimensions]

        fig, ax = plt.subplots(figsize=(9.4, 8.6), subplot_kw=dict(polar=True))
        fig.patch.set_facecolor('#ffffff')
        ax.set_facecolor('#fbfdff')
        fig.subplots_adjust(top=0.82, bottom=0.22, left=0.08, right=0.92)

        if radar_data.max_scores:
            max_vals = radar_data.max_scores + radar_data.max_scores[:1]
            ax.plot(angles, max_vals, color='#9aa7b3', linewidth=1.2, alpha=0.55, linestyle='--', label='满分线')
            ax.fill(angles, max_vals, alpha=0.04, color='#9aa7b3')

        avg_vals = radar_data.class_avg + radar_data.class_avg[:1]
        ax.plot(angles, avg_vals, color='#2f80ed', linewidth=2.4, label='班级均分')
        ax.fill(angles, avg_vals, alpha=0.12, color='#2f80ed')

        colors = ['#e74c3c', '#27ae60', '#f39c12', '#8e44ad', '#16a085']
        for idx, (sid, scores) in enumerate(radar_data.student_scores.items()):
            vals = scores + scores[:1]
            color = colors[idx % len(colors)]
            label = f"学生 {sid[:6]}"
            ax.plot(angles, vals, '-', linewidth=2.2, color=color, label=label)
            ax.fill(angles, vals, alpha=0.10, color=color)

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(labels, fontsize=10, linespacing=1.35)
        ax.tick_params(axis='x', pad=16)
        ax.set_rlabel_position(15)
        ax.tick_params(axis='y', labelsize=9, colors='#6b7280')
        ax.grid(color='#dbe5ef', linewidth=0.8)
        ax.spines['polar'].set_color('#c8d4e0')
        ax.spines['polar'].set_linewidth(1.0)
        ax.set_title('维度能力雷达图', fontsize=17, fontweight='bold', pad=28)
        ax.legend(
            loc='lower center',
            bbox_to_anchor=(0.5, -0.20),
            ncol=min(3, max(1, len(radar_data.student_scores) + 2)),
            frameon=False,
            fontsize=10,
        )

        output_path = os.path.join(
            self.output_dir,
            f"radar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        fig.savefig(
            output_path,
            dpi=170,
            bbox_inches='tight',
            pad_inches=0.55,
            facecolor='white',
            edgecolor='none',
        )
        plt.close(fig)
        logger.info(f"雷达图已生成: {output_path}")
        return output_path

    def _generate_radar_svg(self, radar_data: RadarData) -> str:
        """生成内联 SVG 雷达图，供 HTML 报告直接展示。"""
        if not radar_data.dimensions:
            return ""

        n = len(radar_data.dimensions)
        if n < 3:
            return ""

        width = 660
        height = 560
        cx, cy = width / 2, 240
        radius = 155
        angles = [i / n * 2 * math.pi - math.pi / 2 for i in range(n)]
        max_val = max(radar_data.max_scores) if radar_data.max_scores else 100

        def to_point(idx, val):
            r = (val / max_val) * radius if max_val > 0 else 0
            x = cx + r * math.cos(angles[idx])
            y = cy + r * math.sin(angles[idx])
            return f"{x:.1f},{y:.1f}"

        grid_rings = []
        for level in range(1, 6):
            r = (level / 5) * radius
            points = " ".join(
                f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}"
                for a in angles
            )
            grid_rings.append(
                f'<polygon points="{points}" fill="none" stroke="#d9e2ec" stroke-width="1"/>'
            )

        axis_lines = []
        label_nodes = []
        for i, dim in enumerate(radar_data.dimensions):
            x = cx + radius * math.cos(angles[i])
            y = cy + radius * math.sin(angles[i])
            axis_lines.append(
                f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#c7d2de" stroke-width="1"/>'
            )
            lx = cx + (radius + 40) * math.cos(angles[i])
            ly = cy + (radius + 40) * math.sin(angles[i])
            anchor = 'middle'
            if math.cos(angles[i]) > 0.35:
                anchor = 'start'
            elif math.cos(angles[i]) < -0.35:
                anchor = 'end'
            lines = self._wrap_chart_label(dim, max_chars=6)[:3]
            tspans = ''.join(
                f'<tspan x="{lx:.1f}" dy="{0 if idx == 0 else 14}">{line}</tspan>'
                for idx, line in enumerate(lines)
            )
            label_nodes.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" font-size="12" '
                f'font-weight="600" fill="#334155">{tspans}</text>'
            )

        avg_points = ' '.join(to_point(i, v) for i, v in enumerate(radar_data.class_avg))
        avg_polygon = (
            f'<polygon points="{avg_points}" fill="#2f80ed20" '
            f'stroke="#2f80ed" stroke-width="2.4"/>'
        )

        student_polygons = []
        colors = ['#e74c3c', '#27ae60', '#f39c12', '#8e44ad', '#16a085']
        for idx, (sid, scores) in enumerate(radar_data.student_scores.items()):
            pts = ' '.join(to_point(i, v) for i, v in enumerate(scores))
            color = colors[idx % len(colors)]
            student_polygons.append(
                f'<polygon points="{pts}" fill="{color}22" stroke="{color}" stroke-width="2.2"/>'
            )

        max_points = ' '.join(to_point(i, v) for i, v in enumerate(radar_data.max_scores or [100] * n))

        legend_y = 470
        legend_items = [('#2f80ed', '班级均分'), ('#9aa7b3', '满分线')]
        legend_items.extend((colors[idx % len(colors)], f'学生 {sid[:6]}') for idx, (sid, _) in enumerate(radar_data.student_scores.items()))
        legend_nodes = []
        legend_x = 110
        for idx, (color, label) in enumerate(legend_items):
            x = legend_x + (idx % 3) * 170
            y = legend_y + (idx // 3) * 24
            legend_nodes.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{color}"/>')
            legend_nodes.append(f'<text x="{x + 12}" y="{y + 4}" font-size="11" fill="#334155">{label}</text>')

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="维度能力雷达图">
<rect x="12" y="12" width="{width - 24}" height="{height - 24}" rx="20" fill="#ffffff" stroke="#e6edf5"/>
<text x="{cx}" y="46" text-anchor="middle" font-size="20" font-weight="700" fill="#1f2937">维度能力雷达图</text>
<text x="{cx}" y="68" text-anchor="middle" font-size="12" fill="#64748b">对比班级均分、满分线与学生在各维度上的表现</text>
{''.join(grid_rings)}
{''.join(axis_lines)}
<polygon points="{max_points}" fill="#9aa7b308" stroke="#9aa7b3" stroke-width="1.2" stroke-dasharray="4 4"/>
{avg_polygon}
{''.join(student_polygons)}
{''.join(label_nodes)}
{''.join(legend_nodes)}
</svg>"""
        return svg

    def _generate_pdf(self, data: Dict) -> Optional[str]:
        _patch_hashlib_usedforsecurity_compat()
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

        doc = SimpleDocTemplate(output_path, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=18 * mm, bottomMargin=16 * mm)
        styles = getSampleStyleSheet()

        # 中文字体样式
        cn_title = ParagraphStyle(
            "CNTitle", parent=styles["Title"],
            fontName=font_name, fontSize=20, leading=24, spaceAfter=6,
        )
        cn_normal = ParagraphStyle(
            "CNNormal", parent=styles["Normal"],
            fontName=font_name, fontSize=10, leading=15,
        )
        cn_heading = ParagraphStyle(
            "CNHeading", parent=styles["Heading2"],
            fontName=font_name, fontSize=14, leading=18, spaceBefore=6, spaceAfter=4,
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
        elements.append(Spacer(1, 7 * mm))

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
                img = Image(data["radar_img_path"], width=440, height=400)
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
            dim_table = Table(dim_table_data, colWidths=[170, 68, 68, 68, 52])
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
            }.get(row["appeal_status"], translate_appeal_status(row["appeal_status"]))
            rank = str(row["rank"]) if row.get("rank") else "-"
            table_data.append([
                rank,
                row["student_name"],
                row.get("status_display") or translate_submission_status(row["status"]),
                score,
                plag,
                appeal,
            ])

        col_widths = [36, 86, 66, 52, 38, 62]
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

        detailed_rows = [row for row in data["submissions"] if row.get("grading_evidence")]
        if detailed_rows:
            elements.append(Spacer(1, 6 * mm))
            elements.append(Paragraph("评分依据明细", cn_heading))
            elements.append(Spacer(1, 3 * mm))
            for row in detailed_rows:
                elements.append(Paragraph(
                    f"{row['student_name']}（{row['score'] if row['score'] is not None else '-'}分）",
                    cn_normal,
                ))
                for item in row.get("grading_evidence", []):
                    elements.append(Paragraph(f"  - {item}", cn_small))
                elements.append(Spacer(1, 2 * mm))

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
        """生成 HTML 报告，内联 SVG 雷达图与结构化说明。"""
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

        rows_html = ""
        for row in data["submissions"]:
            score = f"{row['score']:.1f}" if row['score'] is not None else "-"
            plag_icon = "是" if row["plagiarized"] else ""
            appeal_badge = {
                "none": "",
                "pending": '<span class="badge badge-warn">待处理</span>',
                "approved": '<span class="badge badge-good">已批准</span>',
                "rejected": '<span class="badge badge-bad">已驳回</span>',
            }.get(row["appeal_status"], translate_appeal_status(row["appeal_status"]))
            rank = str(row.get("rank", "-"))

            dim_html = ""
            dim_scores = row.get("dimension_scores_display") or row.get("dimension_scores")
            if dim_scores:
                chips = []
                for k, v in dim_scores.items():
                    tone = "good" if v >= 70 else "mid" if v >= 50 else "low"
                    chips.append(f'<span class="dim-chip {tone}">{k}: <b>{v:.1f}</b></span>')
                dim_html = '<div class="dim-chip-row">' + ''.join(chips) + '</div>'

            feedback = localize_user_text(row['feedback'] or '')
            rows_html += f"""
            <tr>
                <td>{rank}</td>
                <td>{row['student_name']}</td>
                <td>{row.get('status_display') or translate_submission_status(row['status'])}</td>
                <td><b>{score}</b></td>
                <td>{plag_icon}</td>
                <td>{appeal_badge}</td>
                <td class="text-left">{feedback[:120]}{dim_html}</td>
            </tr>"""

        grading_evidence_html = ""
        evidence_rows = [row for row in data["submissions"] if row.get("grading_evidence")]
        if evidence_rows:
            cards = []
            for row in evidence_rows:
                row_score = f"{row['score']:.1f}" if row['score'] is not None else "-"
                items = "".join(
                    f"<li>{item}</li>"
                    for item in row.get("grading_evidence", [])
                )
                detail_html = self._build_row_detail_html(row)
                cards.append(f"""
                <article class="detail-card">
                    <div class="detail-head">
                        <h3>{row['student_name']}</h3>
                        <span class="score-pill">{row_score} 分</span>
                    </div>
                    <ul class="detail-list">{items}</ul>
                    {detail_html}
                </article>""")
            grading_evidence_html = '<section class="section"><div class="section-head"><h2>评分依据</h2><p>展示每位学生的维度得分依据与结构化扣分信息。</p></div>' + ''.join(cards) + '</section>'

        dim_stats_html = ""
        if data.get("dim_stats"):
            rows = []
            for dim_name, stats in data["dim_stats"].items():
                avg_color = "#2e7d32" if stats["avg"] >= 70 else "#ef6c00" if stats["avg"] >= 50 else "#c62828"
                rows.append(
                    f'<tr><td class="text-left">{dim_name}</td><td style="color:{avg_color};font-weight:700">{stats["avg"]:.1f}</td><td>{stats["max"]:.1f}</td><td>{stats["min"]:.1f}</td><td>{stats["count"]}</td></tr>'
                )
            dim_stats_html = '<section class="section"><div class="section-head"><h2>维度统计</h2><p>查看各评分维度在班级中的平均、最高、最低分表现。</p></div><div class="table-wrap"><table><tr><th>维度</th><th>平均分</th><th>最高分</th><th>最低分</th><th>人数</th></tr>' + ''.join(rows) + '</table></div></section>'

        radar_html = ""
        radar_data: Optional[RadarData] = data.get("radar_data")
        if radar_data and radar_data.dimensions:
            svg = self._generate_radar_svg(radar_data)
            if svg:
                radar_html = f'<section class="section"><div class="section-head"><h2>维度雷达图</h2><p>对比班级平均水平、满分线和指定学生在各维度上的表现。</p></div><div class="chart-card">{svg}</div></section>'

        evidence_html = ""
        evidences = data.get("evidences")
        if evidences:
            cards = []
            for ev in evidences:
                level_text = {"weak": "薄弱", "medium": "中等", "strong": "良好"}.get(ev.weakness_level, "待观察")
                level_color = {"weak": "#c62828", "medium": "#ef6c00", "strong": "#2e7d32"}.get(ev.weakness_level, "#546e7a")
                refs_html = ""
                if ev.courseware_refs:
                    refs_items = "".join([f'<li>{ref[:150]}</li>' for ref in ev.courseware_refs])
                    refs_html = f'<div class="evidence-block"><h4>课件依据</h4><ul>{refs_items}</ul></div>'
                tips_html = ""
                if ev.improvement_tips:
                    tips_items = "".join([f'<li>{tip}</li>' for tip in ev.improvement_tips])
                    tips_html = f'<div class="evidence-block"><h4>改进建议</h4><ul>{tips_items}</ul></div>'
                cards.append(f"""
                <article class="insight-card">
                    <div class="insight-head">
                        <h3>{ev.dimension_name}</h3>
                        <span class="level-pill" style="background:{level_color}15;color:{level_color};border-color:{level_color}55">{level_text}</span>
                    </div>
                    <p class="metric-line">平均分 {ev.avg_score:.1f} / {ev.max_score:.0f}</p>
                    {refs_html}
                    {tips_html}
                </article>""")
            evidence_html = '<section class="section"><div class="section-head"><h2>维度薄弱项分析</h2><p>根据班级维度表现汇总课件依据与改进建议。</p></div><div class="insight-grid">' + ''.join(cards) + '</div></section>'

        dim_headers = ""
        if data["rubric_dimensions"]:
            dim_headers = ''.join(f'<span class="meta-chip">{name}</span>' for name in data["rubric_dimensions"])

        report_title = "班级批改报告"
        if config.report_type == ReportType.STUDENT_DETAIL:
            report_title = "学生详细报告"
        elif config.report_type == ReportType.RADAR_COMPARISON:
            report_title = "雷达对比报告"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{report_title} - {data['assignment_title']}</title>
<style>
:root {{
  --bg: #f4f7fb;
  --card: #ffffff;
  --line: #dbe4ee;
  --ink: #1f2937;
  --muted: #64748b;
  --brand: #1f6feb;
  --brand-soft: #eaf2ff;
  --good: #2e7d32;
  --mid: #ef6c00;
  --bad: #c62828;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: linear-gradient(180deg, #f7fbff 0%, var(--bg) 100%); color: var(--ink); font-family: "Microsoft YaHei", "PingFang SC", sans-serif; line-height: 1.6; }}
.main {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 48px; }}
.hero {{ background: linear-gradient(135deg, #ffffff 0%, #edf5ff 100%); border: 1px solid var(--line); border-radius: 24px; padding: 28px; box-shadow: 0 18px 48px rgba(31, 41, 55, 0.06); }}
.hero h1 {{ margin: 0 0 10px; font-size: 30px; line-height: 1.25; }}
.hero p {{ margin: 0; color: var(--muted); }}
.meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-top: 22px; }}
.meta-card {{ background: rgba(255,255,255,0.78); border: 1px solid var(--line); border-radius: 16px; padding: 14px 16px; }}
.meta-card .label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
.meta-card .value {{ font-size: 24px; font-weight: 700; }}
.meta-chip-row {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
.meta-chip {{ display: inline-flex; align-items: center; padding: 6px 12px; border-radius: 999px; background: var(--brand-soft); color: #2257b7; font-size: 13px; font-weight: 600; }}
.section {{ margin-top: 26px; background: var(--card); border: 1px solid var(--line); border-radius: 24px; padding: 24px; box-shadow: 0 14px 36px rgba(15, 23, 42, 0.05); }}
.section-head {{ margin-bottom: 18px; }}
.section-head h2 {{ margin: 0 0 6px; font-size: 24px; }}
.section-head p {{ margin: 0; color: var(--muted); }}
.chart-card {{ background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); border: 1px solid #e8eef6; border-radius: 22px; padding: 14px; overflow-x: auto; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
th, td {{ border-bottom: 1px solid #ebf0f5; padding: 12px 14px; text-align: center; vertical-align: top; }}
th {{ background: #f3f7fc; color: #355070; font-size: 13px; text-transform: uppercase; letter-spacing: 0.03em; }}
tr:hover td {{ background: #fbfdff; }}
.text-left {{ text-align: left; }}
.dim-chip-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
.dim-chip {{ display: inline-flex; align-items: center; gap: 4px; padding: 5px 10px; border-radius: 999px; background: #eef3f8; color: #334155; font-size: 12px; }}
.dim-chip.good {{ background: #edf7ee; color: var(--good); }}
.dim-chip.mid {{ background: #fff4e8; color: var(--mid); }}
.dim-chip.low {{ background: #fdecec; color: var(--bad); }}
.badge {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
.badge-good {{ background: #edf7ee; color: var(--good); }}
.badge-warn {{ background: #fff4e8; color: var(--mid); }}
.badge-bad {{ background: #fdecec; color: var(--bad); }}
.insight-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }}
.insight-card, .detail-card {{ background: linear-gradient(180deg, #ffffff 0%, #f9fbfe 100%); border: 1px solid #e8eef6; border-radius: 20px; padding: 18px; }}
.insight-head, .detail-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
.insight-head h3, .detail-head h3 {{ margin: 0; font-size: 18px; }}
.level-pill, .score-pill {{ display: inline-flex; align-items: center; padding: 5px 10px; border-radius: 999px; border: 1px solid transparent; font-size: 12px; font-weight: 700; }}
.score-pill {{ background: #eef4ff; color: #2257b7; border-color: #cfe0ff; }}
.metric-line {{ margin: 0 0 12px; color: var(--muted); font-size: 14px; }}
.evidence-block h4 {{ margin: 10px 0 6px; font-size: 14px; }}
.evidence-block ul, .detail-list {{ margin: 0; padding-left: 18px; }}
.evidence-block li, .detail-list li {{ margin: 6px 0; }}
.footer-note {{ margin-top: 20px; color: var(--muted); font-size: 13px; text-align: right; }}
@media (max-width: 768px) {{
  .main {{ padding: 18px 14px 36px; }}
  .hero {{ padding: 20px; border-radius: 20px; }}
  .hero h1 {{ font-size: 24px; }}
  .section {{ padding: 18px; border-radius: 20px; }}
  .section-head h2 {{ font-size: 20px; }}
}}
</style>
</head>
<body>
<div class="main">
  <section class="hero">
    <h1>{report_title}</h1>
    <p>{data['assignment_title']}</p>
    <div class="meta-grid">
      <div class="meta-card"><span class="label">提交人数</span><span class="value">{data['total_submissions']}</span></div>
      <div class="meta-card"><span class="label">平均分</span><span class="value">{data['avg_score']}</span></div>
      <div class="meta-card"><span class="label">中位数</span><span class="value">{data.get('median_score', 0)}</span></div>
      <div class="meta-card"><span class="label">最高分</span><span class="value">{data['max_score']}</span></div>
      <div class="meta-card"><span class="label">最低分</span><span class="value">{data['min_score']}</span></div>
    </div>
    <div class="meta-chip-row">{dim_headers}</div>
    <p class="footer-note">生成时间：{data['generated_at']}</p>
  </section>
  {radar_html}
  {dim_stats_html}
  {evidence_html}
  {grading_evidence_html}
  <section class="section">
    <div class="section-head"><h2>学生明细</h2><p>查看学生分数、状态、申诉情况与评语摘要。</p></div>
    <div class="table-wrap">
      <table>
        <tr><th>排名</th><th>学生</th><th>状态</th><th>总分</th><th>查重</th><th>申诉</th><th>评语摘要</th></tr>
        {rows_html}
      </table>
    </div>
  </section>
</div>
</body>
</html>"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"HTML 报告已生成: {output_path}")
        return output_path

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
