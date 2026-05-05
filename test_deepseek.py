"""
DeepSeek 真实 AI 批改测试
需要 .env 中配置 DEEPSEEK_API_KEY
"""
from __future__ import annotations
import os
import sys
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

# 添加 src 到路径
SRC = Path(__file__).parent / "src"
sys.path.insert(0, str(SRC))

# 先加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from contracts.models import (
    IntentType, MessageType, SubmitStatus,
    RubricDimension, QQMessage, IntentResult,
    Rubric, Assignment, HomeworkSubmission,
)
from storage.db import DB
from grading.engine import GradingEngine
from rubric.rubric_parser import RubricParser
from homework.receiver import HomeworkReceiver


async def main():
    print("=" * 60)
    print(" DeepSeek 真实 AI 批改测试")
    print("=" * 60)

    # 检查 API Key
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
    oa_key = os.environ.get("OPENAI_API_KEY", "")
    if not ds_key and not oa_key:
        print("\n❌ 未配置 API Key!")
        print("   请在 .env 文件中设置 DEEPSEEK_API_KEY=your-key")
        print("   或者设置 OPENAI_API_KEY=your-key")
        sys.exit(1)

    provider = "DeepSeek" if ds_key else "OpenAI"
    key_preview = (ds_key or oa_key)[:8] + "..."
    print(f"\n✅ 检测到 {provider} API Key: {key_preview}")

    # 初始化
    DB_PATH = "test_deepseek.db"
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    db = DB(DB_PATH)
    engine = GradingEngine(db)
    rubric_parser = RubricParser()
    homework = HomeworkReceiver(db)

    print(f"\n📋 批改引擎配置:")
    print(f"   Provider: {engine._provider}")
    print(f"   Model:    {engine.model}")
    print(f"   Base URL: {engine._base_url or 'default'}")

    # 创建评分标准
    rubric_text = (
        "内容准确性:0.4:答案是否正确，是否覆盖关键知识点\n"
        "逻辑性:0.3:推理是否合理，论证是否严密\n"
        "表达:0.3:语言是否清晰，结构是否完整\n"
        "硬扣分:迟到扣10分"
    )
    rubric = rubric_parser.parse(rubric_text, title="数据库实验评分标准")
    db.save_rubric(rubric)
    print(f"\n✅ 评分标准已创建: {len(rubric.dimensions)} 个维度")

    # 创建作业
    assignment = type("A", (), {
        "id": "ASGN_DS01", "title": "Access数据库实验报告",
        "description": "完成Access数据库设计", "rubric_id": rubric.id,
        "courseware_id": None,
        "deadline": datetime.now() + timedelta(days=7),
        "max_score": 100.0, "created_at": datetime.now(),
    })()
    db.save_assignment(assignment)

    # 提交不同质量的作业
    submissions_data = [
        ("U2001", "张三-优秀", (
            "Access数据库实验报告\n\n"
            "一、实验目的\n"
            "本实验旨在掌握Access数据库的基本操作，包括创建表、设计查询和生成报表。\n\n"
            "二、实验内容\n"
            "1. 创建了学生信息表，包含学号、姓名、性别、年龄等字段，设置了学号为主键。\n"
            "2. 设计了选课查询，通过SQL语句实现了多表关联查询：SELECT 学生.姓名, 课程.课程名 FROM 学生 INNER JOIN 选课 ON 学生.学号=选课.学号。\n"
            "3. 创建了成绩报表，按班级分组统计平均分和最高分。\n\n"
            "三、实验结论\n"
            "通过本次实验，我深入理解了关系型数据库的设计原理。Access提供了可视化的查询设计器，"
            "但掌握SQL语句对于复杂查询更为高效。主键约束确保了数据完整性，外键关联实现了表间关系。"
        )),
        ("U2002", "李四-一般", (
            "Access实验报告\n\n"
            "我做了数据库实验，创建了一个表，然后做了一些查询。"
            "实验挺简单的，就是按照步骤操作就行了。"
        )),
        ("U2003", "王五-差", "做了"),
    ]

    print(f"\n{'='*60}")
    print(" 开始 AI 批改")
    print(f"{'='*60}")

    for student_id, name, content in submissions_data:
        sub = homework.receive(
            assignment_id="ASGN_DS01",
            student_id=student_id,
            student_name=name,
            content=content,
        )
        print(f"\n📝 批改 {name} 的作业 (提交ID: {sub.id})...")
        print(f"   内容长度: {len(content)} 字符")

        result = engine.grade(sub, rubric, "Access数据库实验报告")

        print(f"   ✅ 批改完成!")
        print(f"   总分: {result.total_score} 分")
        print(f"   置信度: {result.confidence:.2f}")
        print(f"   批改方式: {result.graded_by}")
        print(f"   维度评分:")
        for dim_name, score in result.dimension_scores.items():
            dim = next((d for d in rubric.dimensions if d.name == dim_name), None)
            weight_str = f" (权重{dim.weight:.0%})" if dim else ""
            print(f"     • {dim_name}: {score}分{weight_str}")
        print(f"   反馈: {result.feedback[:200]}")

    # 申诉重批测试
    print(f"\n{'='*60}")
    print(" 申诉重批测试")
    print(f"{'='*60}")

    sub_wang = db.get_student_submission("ASGN_DS01", "U2003")
    if sub_wang and engine.client:
        from appeal.handler import AppealHandler
        appeal_h = AppealHandler(db, engine)

        appeal = appeal_h.submit_appeal(sub_wang.id, "U2003", "我只写了两个字但我觉得内容是完整的")
        print(f"\n📝 申诉已提交: {appeal.id}")

        result2 = appeal_h.process_appeal(appeal.id, approved=True, reviewer_id="TA01")
        print(f"   ✅ 申诉重批完成! 新分数: {result2.new_score}")

    # 汇总
    print(f"\n{'='*60}")
    print(" 批改汇总")
    print(f"{'='*60}")
    subs = db.list_submissions("ASGN_DS01")
    for s in subs:
        gr = db.get_grading_result(s.id)
        grade_by = gr.graded_by if gr else "?"
        print(f"  {s.student_name}: {s.score}分 ({grade_by})")

    # 清理
    try:
        os.remove(DB_PATH)
    except Exception:
        pass

    print(f"\n{'='*60}")
    print(" 测试完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
