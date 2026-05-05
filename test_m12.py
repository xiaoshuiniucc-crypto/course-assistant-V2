"""
M12 报告生成增强测试
雷达图 + 课件依据 + 学生排名 + 改进建议
"""
import os
import sys
import json
import shutil
import tempfile
from datetime import datetime

# 添加 src 到 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from storage.db import DB
from report.report_gen import ReportGenerator
from contracts.models import (
    Courseware, Rubric, RubricDimension, Assignment,
    HomeworkSubmission, GradingResult, Appeal,
    ReportConfig, ReportEvidence, RadarData, ReportType,
    SubmitStatus, IntentType
)


# ── 测试辅助 ────────────────────────────────

passed = 0
failed = 0
errors = []
_tmpdirs = []


def log(section: str, ok: bool, detail: str):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✅ [{section}] {detail}")
    else:
        failed += 1
        errors.append(f"[{section}] {detail}")
        print(f"  ❌ [{section}] {detail}")


def make_tmpdir(prefix="m12_") -> str:
    """创建临时目录，统一管理清理"""
    d = tempfile.mkdtemp(prefix=prefix)
    _tmpdirs.append(d)
    return d


def cleanup_all():
    """清理所有临时目录"""
    for d in _tmpdirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def setup_test_data(db: DB):
    """创建测试数据：课件 + 评分标准 + 作业 + 提交 + 批改结果"""
    # 课件
    cw = Courseware(
        id="cw_test",
        title="Python基础教程",
        content="Python是一种高级编程语言。变量是存储数据的容器。函数是可重用的代码块。"
               "面向对象编程包括类和对象。异常处理使用try-except语句。"
               "文件操作使用open函数。列表推导式是Python的特色语法。",
        file_path="",
        uploaded_by="teacher_001",
        created_at=datetime.now(),
        meta={"topics": ["变量", "函数", "面向对象", "异常处理", "文件操作"]},
    )
    db.save_courseware(cw)

    # 评分标准（4个维度）
    rubric = Rubric(
        id="rub_test",
        title="Python作业评分标准",
        dimensions=[
            RubricDimension(name="代码正确性", weight=0.35, max_score=100,
                           description="代码能否正确运行并输出预期结果"),
            RubricDimension(name="代码风格", weight=0.20, max_score=100,
                           description="代码是否遵循PEP8规范"),
            RubricDimension(name="算法效率", weight=0.25, max_score=100,
                           description="算法的时间和空间复杂度"),
            RubricDimension(name="文档与注释", weight=0.20, max_score=100,
                           description="是否有充分的文档和注释"),
        ],
        total_score=100.0,
    )
    db.save_rubric(rubric)

    # 作业
    asgn = Assignment(
        id="asgn_m12",
        title="Python期中作业",
        description="编写一个Python程序",
        rubric_id=rubric.id,
        courseware_id=cw.id,
        deadline=None,
        created_at=datetime.now(),
    )
    db.save_assignment(asgn)

    # 5个学生的提交和批改
    students = [
        ("stu_001", "张三", 85, {"代码正确性": 90, "代码风格": 80, "算法效率": 85, "文档与注释": 82}),
        ("stu_002", "李四", 72, {"代码正确性": 78, "代码风格": 70, "算法效率": 65, "文档与注释": 75}),
        ("stu_003", "王五", 93, {"代码正确性": 95, "代码风格": 92, "算法效率": 90, "文档与注释": 95}),
        ("stu_004", "赵六", 58, {"代码正确性": 55, "代码风格": 50, "算法效率": 60, "文档与注释": 70}),
        ("stu_005", "钱七", 76, {"代码正确性": 80, "代码风格": 72, "算法效率": 78, "文档与注释": 68}),
    ]

    for sid, name, total, dim_scores in students:
        sub = HomeworkSubmission(
            id=f"sub_{sid}",
            assignment_id=asgn.id,
            student_id=sid,
            student_name=name,
            content=f"{name}的作业内容",
            file_path=None,
            status=SubmitStatus.GRADED,
            submitted_at=datetime.now(),
            score=total,
            feedback=f"总体评价: {'良好' if total >= 80 else '需要改进'}",
            graded_at=datetime.now(),
        )
        db.save_submission(sub)

        result = GradingResult(
            submission_id=sub.id,
            dimension_scores=dim_scores,
            total_score=total,
            feedback=sub.feedback,
            confidence=0.85,
            graded_by="ai",
            graded_at=datetime.now(),
        )
        db.save_grading_result(result)


# ── T1: ReportConfig 数据模型 ────────────────

def test_report_config():
    print("\n📋 T1: ReportConfig 数据模型")

    config = ReportConfig(assignment_id="asgn_001")
    log("T1", config.assignment_id == "asgn_001", "基本字段赋值")
    log("T1", config.report_type == ReportType.CLASS_SUMMARY, "默认类型为班级汇总")
    log("T1", config.include_radar is True, "默认包含雷达图")
    log("T1", config.include_evidence is True, "默认包含课件依据")
    log("T1", config.student_id is None, "默认无学生ID")
    log("T1", config.comparison_students == [], "默认无对比学生")

    config2 = ReportConfig(
        assignment_id="asgn_002",
        report_type=ReportType.STUDENT_DETAIL,
        student_id="stu_001",
        include_radar=True,
        include_evidence=True,
    )
    log("T1", config2.report_type == ReportType.STUDENT_DETAIL, "学生详细报告类型")
    log("T1", config2.student_id == "stu_001", "指定学生ID")


# ── T2: ReportEvidence 数据模型 ────────────────

def test_report_evidence():
    print("\n📋 T2: ReportEvidence 数据模型")

    ev = ReportEvidence(
        dimension_name="代码正确性",
        avg_score=78.5,
        max_score=100,
        weakness_level="medium",
        courseware_refs=["课件第3章: 变量与数据类型", "课件第5章: 函数定义"],
        improvement_tips=["建议加强变量与数据类型章节的练习"],
    )
    log("T2", ev.dimension_name == "代码正确性", "维度名称")
    log("T2", ev.avg_score == 78.5, "平均分")
    log("T2", ev.weakness_level == "medium", "薄弱程度")
    log("T2", len(ev.courseware_refs) == 2, "课件引用数量")
    log("T2", len(ev.improvement_tips) == 1, "改进建议数量")


# ── T3: RadarData 数据模型 ────────────────

def test_radar_data():
    print("\n📋 T3: RadarData 数据模型")

    rd = RadarData(
        dimensions=["代码正确性", "代码风格", "算法效率", "文档与注释"],
        class_avg=[80, 75, 70, 78],
        student_scores={"stu_001": [90, 80, 85, 82]},
        max_scores=[100, 100, 100, 100],
    )
    log("T3", len(rd.dimensions) == 4, "维度数量")
    log("T3", len(rd.class_avg) == 4, "班级平均分数量")
    log("T3", "stu_001" in rd.student_scores, "学生分数键存在")
    log("T3", len(rd.student_scores["stu_001"]) == 4, "学生维度分数数量")
    log("T3", len(rd.max_scores) == 4, "满分数量")


# ── T4: DB 新表测试 ────────────────

def test_db_new_tables():
    print("\n📋 T4: DB 新表测试")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_m12.db"))

    # 保存报告记录
    config = ReportConfig(assignment_id="asgn_test")
    db.save_report(
        report_id="rpt_001",
        assignment_id="asgn_test",
        report_type=ReportType.CLASS_SUMMARY,
        file_path="/tmp/report.pdf",
        config=config,
        radar_included=True,
        evidence_included=True,
    )
    report = db.get_report("rpt_001")
    log("T4", report is not None, "保存并获取报告记录")
    log("T4", report["report_type"] == ReportType.CLASS_SUMMARY, "报告类型正确")
    log("T4", report["radar_included"] is True, "雷达图标记正确")
    log("T4", report["evidence_included"] is True, "课件依据标记正确")

    # 保存课件依据
    ev = ReportEvidence(
        dimension_name="代码正确性",
        avg_score=78.5,
        max_score=100,
        weakness_level="medium",
        courseware_refs=["课件章节1"],
        improvement_tips=["建议加强练习"],
    )
    db.save_report_evidence("rpt_001", ev)
    evidences = db.get_report_evidences("rpt_001")
    log("T4", len(evidences) == 1, "课件依据保存和获取")
    log("T4", evidences[0].dimension_name == "代码正确性", "维度名称正确")
    log("T4", evidences[0].weakness_level == "medium", "薄弱程度正确")

    # 列出报告
    reports = db.list_reports(assignment_id="asgn_test")
    log("T4", len(reports) == 1, "列出报告")

    # 维度统计
    setup_test_data(db)
    dim_stats = db.get_dimension_stats("asgn_m12")
    log("T4", len(dim_stats) == 4, "维度统计数量为4")
    log("T4", "代码正确性" in dim_stats, "维度统计包含代码正确性")
    log("T4", dim_stats["代码正确性"]["avg"] > 0, "代码正确性平均分大于0")

    # 学生个人报告
    config2 = ReportConfig(
        assignment_id="asgn_test2",
        report_type=ReportType.STUDENT_DETAIL,
        student_id="stu_001",
    )
    db.save_report(
        report_id="rpt_002",
        assignment_id="asgn_test2",
        report_type=ReportType.STUDENT_DETAIL,
        file_path="/tmp/student_report.pdf",
        config=config2,
        student_id="stu_001",
    )
    reports_by_student = db.list_reports(student_id="stu_001")
    log("T4", len(reports_by_student) >= 1, "按学生筛选报告")

    db.close()


# ── T5: 雷达图数据收集 ────────────────

def test_radar_data_collection():
    print("\n📋 T5: 雷达图数据收集")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_radar.db"))
    setup_test_data(db)
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    # 收集雷达图数据
    radar_data = gen._collect_radar_data("asgn_m12")
    log("T5", len(radar_data.dimensions) == 4, "维度数量为4")
    log("T5", len(radar_data.class_avg) == 4, "班级平均分数量为4")
    log("T5", len(radar_data.max_scores) == 4, "满分数量为4")

    # 验证班级平均分在合理范围
    for avg in radar_data.class_avg:
        log("T5", 0 <= avg <= 100, f"班级平均分 {avg} 在0-100范围")

    # 带学生ID的雷达图数据
    radar_data_with_student = gen._collect_radar_data(
        "asgn_m12", student_ids=["stu_001", "stu_003"]
    )
    log("T5", "stu_001" in radar_data_with_student.student_scores,
        "学生001的雷达图数据存在")
    log("T5", "stu_003" in radar_data_with_student.student_scores,
        "学生003的雷达图数据存在")
    log("T5", len(radar_data_with_student.student_scores["stu_001"]) == 4,
        "学生001有4个维度分数")

    # 无评分标准的情况
    radar_empty = gen._collect_radar_data("nonexistent")
    log("T5", len(radar_empty.dimensions) == 0, "不存在作业返回空数据")

    db.close()


# ── T6: 课件依据收集 ────────────────

def test_evidence_collection():
    print("\n📋 T6: 课件依据收集")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_ev.db"))
    setup_test_data(db)

    # 无知识库
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))
    radar_data = gen._collect_radar_data("asgn_m12")
    evidences = gen._collect_evidences("asgn_m12", radar_data)

    log("T6", len(evidences) == 4, "课件依据数量等于维度数量")

    # 验证每个依据的字段
    for ev in evidences:
        log("T6", ev.dimension_name in ["代码正确性", "代码风格", "算法效率", "文档与注释"],
            f"维度名称: {ev.dimension_name}")
        log("T6", ev.weakness_level in ["weak", "medium", "strong"],
            f"薄弱程度: {ev.weakness_level}")
        log("T6", ev.max_score == 100, f"满分: {ev.max_score}")
        log("T6", len(ev.improvement_tips) >= 1, "有改进建议")

    # 弱维度检测
    weak_dims = [e for e in evidences if e.weakness_level == "weak"]
    medium_dims = [e for e in evidences if e.weakness_level == "medium"]
    strong_dims = [e for e in evidences if e.weakness_level == "strong"]
    log("T6", len(weak_dims) + len(medium_dims) + len(strong_dims) == 4,
        "所有维度都有薄弱等级分类")

    db.close()


# ── T7: 班级汇总报告生成 ────────────────

def test_class_summary_report():
    print("\n📋 T7: 班级汇总报告生成")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_cls.db"))
    setup_test_data(db)
    out_dir = os.path.join(tmpdir, "reports")
    gen = ReportGenerator(db, output_dir=out_dir)

    config = ReportConfig(
        assignment_id="asgn_m12",
        report_type=ReportType.CLASS_SUMMARY,
        include_radar=True,
        include_evidence=True,
    )
    path = gen.generate_with_config(config)

    log("T7", path is not None, "报告生成成功")
    log("T7", os.path.exists(path), f"报告文件存在: {path}")
    log("T7", path.endswith(".pdf") or path.endswith(".html"),
        "报告格式为PDF或HTML")

    # 数据包含排名
    data = gen._collect_data("asgn_m12")
    ranked = [s for s in data["submissions"] if s.get("rank")]
    log("T7", len(ranked) == 5, f"有排名的学生数: {len(ranked)}")

    # 排名顺序
    ranks = [s["rank"] for s in ranked]
    log("T7", ranks == sorted(ranks), "排名按分数从高到低排序")

    # 中位数
    log("T7", data.get("median_score", 0) > 0, f"中位数: {data.get('median_score')}")

    db.close()


# ── T8: 学生个人报告生成 ────────────────

def test_student_detail_report():
    print("\n📋 T8: 学生个人报告生成")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_stu.db"))
    setup_test_data(db)
    out_dir = os.path.join(tmpdir, "reports")
    gen = ReportGenerator(db, output_dir=out_dir)

    path = gen.generate_student_report("asgn_m12", "stu_001")
    log("T8", path is not None, "学生报告生成成功")
    log("T8", os.path.exists(path), f"学生报告文件存在: {path}")

    # 验证数据只包含指定学生
    config = ReportConfig(
        assignment_id="asgn_m12",
        report_type=ReportType.STUDENT_DETAIL,
        student_id="stu_001",
    )
    data = gen._collect_data("asgn_m12")
    # 注意：学生筛选在 _generate_report 中进行，不在 _collect_data 中
    log("T8", data["total_submissions"] == 5, "原始数据包含所有学生")

    # 生成多个学生报告
    path2 = gen.generate_student_report("asgn_m12", "stu_003")
    log("T8", path2 is not None, "第二个学生报告生成成功")

    db.close()


# ── T9: 雷达图对比报告 ────────────────

def test_radar_comparison_report():
    print("\n📋 T9: 雷达图对比报告")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_cmp.db"))
    setup_test_data(db)
    out_dir = os.path.join(tmpdir, "reports")
    gen = ReportGenerator(db, output_dir=out_dir)

    path = gen.generate_radar_comparison(
        "asgn_m12", ["stu_001", "stu_003", "stu_004"]
    )
    log("T9", path is not None, "对比报告生成成功")
    log("T9", os.path.exists(path), f"对比报告文件存在: {path}")

    db.close()


# ── T10: SVG 雷达图生成 ────────────────

def test_radar_svg():
    print("\n📋 T10: SVG 雷达图生成")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_svg.db"))
    setup_test_data(db)
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    radar_data = gen._collect_radar_data(
        "asgn_m12", student_ids=["stu_001"]
    )
    svg = gen._generate_radar_svg(radar_data)

    log("T10", len(svg) > 0, "SVG 非空")
    log("T10", "<svg" in svg, "包含 <svg> 标签")
    log("T10", "代码正确性" in svg, "SVG 包含维度标签")
    log("T10", "班级平均" in svg, "SVG 包含图例")
    log("T10", "polygon" in svg, "SVG 包含多边形")

    # 空数据
    empty_svg = gen._generate_radar_svg(RadarData(dimensions=[], class_avg=[]))
    log("T10", empty_svg == "", "空数据返回空 SVG")

    # 少于3个维度
    small_radar = RadarData(
        dimensions=["A", "B"],
        class_avg=[50, 60],
    )
    small_svg = gen._generate_radar_svg(small_radar)
    log("T10", small_svg == "", "少于3维度返回空 SVG")

    db.close()


# ── T11: 改进建议生成 ────────────────

def test_improvement_tips():
    print("\n📋 T11: 改进建议生成")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_tips.db"))
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    # weak
    tips_weak = gen._generate_tips("代码正确性", "weak", 0.4)
    log("T11", len(tips_weak) == 2, f"weak 建议2条, 实际: {len(tips_weak)}")
    log("T11", "偏低" in tips_weak[0], "weak 建议包含'偏低'")

    # medium
    tips_medium = gen._generate_tips("代码风格", "medium", 0.6)
    log("T11", len(tips_medium) == 2, f"medium 建议2条")
    log("T11", "中等" in tips_medium[0], "medium 建议包含'中等'")

    # strong
    tips_strong = gen._generate_tips("算法效率", "strong", 0.9)
    log("T11", len(tips_strong) == 1, f"strong 建议1条")
    log("T11", "良好" in tips_strong[0], "strong 建议包含'良好'")

    db.close()


# ── T12: 报告记录保存 ────────────────

def test_report_db_record():
    print("\n📋 T12: 报告记录保存与查询")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_rec.db"))
    setup_test_data(db)
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    # 生成报告（会自动保存记录）
    config = ReportConfig(
        assignment_id="asgn_m12",
        include_radar=True,
        include_evidence=True,
    )
    path = gen.generate_with_config(config)

    # 查询报告记录
    reports = db.list_reports(assignment_id="asgn_m12")
    log("T12", len(reports) >= 1, f"报告记录数: {len(reports)}")

    if reports:
        r = reports[0]
        log("T12", r["assignment_id"] == "asgn_m12", "作业ID正确")
        log("T12", r["file_path"] == path, "文件路径正确")

    # 课件依据
    if reports:
        evidences = db.get_report_evidences(reports[0]["id"])
        log("T12", len(evidences) >= 1, f"课件依据数: {len(evidences)}")

    db.close()


# ── T13: 缓存机制 ────────────────

def test_cache_mechanism():
    print("\n📋 T13: 缓存机制")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_cache.db"))
    setup_test_data(db)
    out_dir = os.path.join(tmpdir, "reports")
    gen = ReportGenerator(db, output_dir=out_dir)

    # 生成并缓存
    path1 = gen.get_or_generate("asgn_m12")
    path2 = gen.get_or_generate("asgn_m12")
    log("T13", path1 == path2, "缓存命中，路径一致")

    # 失效后重新生成
    gen.invalidate("asgn_m12")
    path3 = gen.get_or_generate("asgn_m12", force=True)
    log("T13", path3 is not None, "失效后重新生成成功")
    log("T13", os.path.exists(path3), "新报告文件存在")

    # 不同配置不同缓存
    path_class = gen.generate_with_config(ReportConfig(
        assignment_id="asgn_m12",
        report_type=ReportType.CLASS_SUMMARY,
    ))
    path_student = gen.generate_student_report("asgn_m12", "stu_001")
    log("T13", path_class != path_student, "不同配置生成不同报告")

    db.close()


# ── T14: 中位数计算 ────────────────

def test_median():
    print("\n📋 T14: 中位数计算")

    log("T14", ReportGenerator._median([1, 2, 3]) == 2, "奇数个中位数")
    log("T14", ReportGenerator._median([1, 2, 3, 4]) == 2.5, "偶数个中位数")
    log("T14", ReportGenerator._median([]) == 0, "空列表中位数为0")
    log("T14", ReportGenerator._median([42]) == 42, "单元素中位数")


# ── T15: HTML 报告内容验证 ────────────────

def test_html_report_content():
    print("\n📋 T15: HTML 报告内容验证")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_html.db"))
    setup_test_data(db)
    out_dir = os.path.join(tmpdir, "reports")
    gen = ReportGenerator(db, output_dir=out_dir)

    config = ReportConfig(
        assignment_id="asgn_m12",
        include_radar=True,
        include_evidence=True,
    )
    # 强制使用 HTML（通过让 PDF 生成失败）
    original_pdf = gen._generate_pdf
    gen._generate_pdf = lambda data: None  # noqa: 强制 fallback

    path = gen._generate_report(config)
    log("T15", path.endswith(".html"), f"生成 HTML 报告: {path}")

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        log("T15", "<svg" in content, "HTML 包含 SVG 雷达图")
        log("T15", "课件依据" in content, "HTML 包含课件依据标题")
        log("T15", "改进建议" in content, "HTML 包含改进建议标题")
        log("T15", "排名" in content, "HTML 包含排名")
        log("T15", "中位数" in content, "HTML 包含中位数")
        log("T15", "维度统计" in content, "HTML 包含维度统计")

    # 恢复
    gen._generate_pdf = original_pdf

    db.close()


# ── T16: 旧接口兼容 ────────────────

def test_backward_compatibility():
    print("\n📋 T16: 旧接口兼容性")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_compat.db"))
    setup_test_data(db)
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    # 旧接口 get_or_generate
    path = gen.get_or_generate("asgn_m12")
    log("T16", path is not None, "get_or_generate 仍然可用")
    log("T16", os.path.exists(path), "旧接口生成的文件存在")

    # 旧接口 invalidate
    gen.invalidate("asgn_m12")
    path2 = gen.get_or_generate("asgn_m12", force=True)
    log("T16", path2 is not None, "invalidate 后重新生成成功")

    db.close()


# ── T17: IntentType.GENERATE_STUDENT_REPORT ────────────────

def test_intent_type():
    print("\n📋 T17: GENERATE_STUDENT_REPORT 意图类型")

    log("T17", hasattr(IntentType, "GENERATE_STUDENT_REPORT"),
        "IntentType 包含 GENERATE_STUDENT_REPORT")
    log("T17", IntentType.GENERATE_STUDENT_REPORT == "generate_student_report",
        "意图类型值正确")


# ── T18: ReportType 常量 ────────────────

def test_report_type_constants():
    print("\n📋 T18: ReportType 常量")

    log("T18", ReportType.CLASS_SUMMARY == "class_summary", "班级汇总类型")
    log("T18", ReportType.STUDENT_DETAIL == "student_detail", "学生详情类型")
    log("T18", ReportType.RADAR_COMPARISON == "radar_comparison", "雷达对比类型")
    log("T18", ReportType.COURSEWARE_EVIDENCE == "courseware_evidence", "课件依据类型")


# ── T19: 空数据报告 ────────────────

def test_empty_data_report():
    print("\n📋 T19: 空数据报告")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_empty.db"))
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    # 无作业数据
    config = ReportConfig(assignment_id="nonexistent")
    path = gen.generate_with_config(config)
    log("T19", path is not None, "空数据也能生成报告")
    log("T19", os.path.exists(path), "空数据报告文件存在")

    db.close()


# ── T20: 维度统计完整性 ────────────────

def test_dimension_stats():
    print("\n📋 T20: 维度统计完整性")

    tmpdir = make_tmpdir()
    db = DB(os.path.join(tmpdir, "test_stats.db"))
    setup_test_data(db)
    gen = ReportGenerator(db, output_dir=os.path.join(tmpdir, "reports"))

    dim_stats = db.get_dimension_stats("asgn_m12")

    log("T20", len(dim_stats) == 4, f"维度数: {len(dim_stats)}")
    for dim_name, stats in dim_stats.items():
        log("T20", "avg" in stats, f"{dim_name}: 有平均分")
        log("T20", "max" in stats, f"{dim_name}: 有最高分")
        log("T20", "min" in stats, f"{dim_name}: 有最低分")
        log("T20", "count" in stats, f"{dim_name}: 有人数")
        log("T20", stats["count"] == 5, f"{dim_name}: 人数=5")

    # 验证具体数值（代码正确性: 90, 78, 95, 55, 80 → avg=79.6）
    avg_correctness = dim_stats["代码正确性"]["avg"]
    log("T20", 79 <= avg_correctness <= 81,
        f"代码正确性平均分: {avg_correctness}")

    db.close()


# ── 运行所有测试 ────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("M12 报告生成增强测试")
    print("=" * 60)

    try:
        test_report_config()
        test_report_evidence()
        test_radar_data()
        test_db_new_tables()
        test_radar_data_collection()
        test_evidence_collection()
        test_class_summary_report()
        test_student_detail_report()
        test_radar_comparison_report()
        test_radar_svg()
        test_improvement_tips()
        test_report_db_record()
        test_cache_mechanism()
        test_median()
        test_html_report_content()
        test_backward_compatibility()
        test_intent_type()
        test_report_type_constants()
        test_empty_data_report()
        test_dimension_stats()

        print("\n" + "=" * 60)
        print(f"📊 M12 测试结果: ✅ {passed} 通过, ❌ {failed} 失败")
        print("=" * 60)

        if errors:
            print("\n失败详情:")
            for e in errors:
                print(f"  ❌ {e}")
    finally:
        cleanup_all()

    sys.exit(0 if failed == 0 else 1)
