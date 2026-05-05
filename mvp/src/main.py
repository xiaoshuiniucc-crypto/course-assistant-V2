"""
QQ课程AI助手 — MVP 主入口
端到端流程演示：上传课件 → 设置细则 → 提交作业 → AI批改 → 查看结果 → 申诉

使用方式：
  cd D:\QQ课程AI助手\mvp\src
  python main.py

需要设置环境变量：
  OPENAI_API_KEY=sk-xxx
  OPENAI_BASE_URL=https://xxx  (可选，默认用 OpenAI 官方)
"""

import sys
import os

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage.db import init_db, upsert_assignment, save_rubric, get_latest_rubric
from rubric.rubric_parser import parse as parse_rubric
from homework.receiver import receive_file, get_submission_text
from grading.engine import grade, LLMClient
from notify.result_notifier import format_summary, print_result
from appeal.handler import submit_appeal, trigger_regrade, resolve_appeal_interactive, get_appeal_summary
from mvp_parser.file_parser import parse as parse_file


# ── 示例评分细则文本 ────────────────────────────────────────

SAMPLE_RUBRIC_TEXT = """
## 作业名称：数据库第二次作业
## 总分：100分
## 截止时间：2026-05-10 23:59

### 评分维度
| 维度 | 满分 | 评分要点 |
|------|------|----------|
| 问题分析 | 30 | 是否准确识别核心问题，是否结合课件概念 |
| 方案设计 | 40 | 方案完整性、逻辑合理性、是否考虑边界条件 |
| 表达规范 | 30 | 结构清晰、引用正确、术语使用准确 |

### 硬性扣分项
- 抄袭：直接判0分
- 未按格式提交：扣10分
"""

# ── 示例课件文本 ────────────────────────────────────────────

SAMPLE_COURSEWARE = """
第3章 数据库设计

3.1 需求分析
需求分析是数据库设计的起点，主要任务包括：
- 确定用户的数据需求、处理需求、安全性需求和完整性需求
- 需求分析的步骤：需求收集、需求分析、需求表达、需求验证

3.2 概念结构设计
概念结构设计是将需求分析得到的用户需求抽象为信息结构（概念模型）的过程。
- E-R模型是概念结构设计的主要工具
- 实体：客观存在并可相互区别的事物
- 属性：实体所具有的某一特性
- 联系：实体之间的关联关系

3.3 逻辑结构设计
逻辑结构设计是将概念结构转换为某个DBMS所支持的数据模型的过程。
- E-R图向关系模式的转换规则
- 数据模型的优化：规范化理论

3.4 物理结构设计
物理结构设计是为一个给定的逻辑数据模型选取一个最适合应用环境的物理结构的过程。
- 存储结构设计
- 存取方法设计

3.5 约束条件
约束条件是系统设计的前置要素，包括：
- 实体完整性约束
- 参照完整性约束
- 用户定义的完整性约束
"""


def run_demo():
    """运行完整 MVP 演示流程。"""
    print("=" * 60)
    print("  QQ课程AI助手 — MVP 端到端演示")
    print("=" * 60)

    # ── Step 1: 初始化 ─────────────────────────────
    print("\n📦 Step 1: 初始化数据库...")
    init_db()
    print("   ✓ 数据库已就绪")

    # ── Step 2: 创建课程和作业 ─────────────────────
    print("\n📚 Step 2: 创建课程作业...")
    upsert_assignment("C001", "A001", "数据库第二次作业", "2026-05-10T23:59")
    print("   ✓ 课程C001 - 作业A001 已创建")

    # ── Step 3: 解析评分细则 ────────────────────────
    print("\n📋 Step 3: 解析评分细则...")
    rubric = parse_rubric(SAMPLE_RUBRIC_TEXT, "#评分细则", "C001", "A001")
    save_rubric(rubric)
    print(f"   ✓ 细则解析完成：{rubric.title}")
    print(f"   维度：{', '.join(d.name for d in rubric.dimensions)}")
    print(f"   总分：{rubric.total_score}")

    # ── Step 4: 初始化 LLM 客户端 ──────────────────
    print("\n🤖 Step 4: 初始化 AI 批改引擎...")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("   ⚠️ 未设置 OPENAI_API_KEY 环境变量")
        print("   请先运行：set OPENAI_API_KEY=sk-xxx")
        print("   或设置 OPENAI_BASE_URL 使用兼容 API")
        api_key = input("   请输入 API Key（或按回车退出）：").strip()
        if not api_key:
            print("   退出。")
            return
        os.environ["OPENAI_API_KEY"] = api_key

    llm = LLMClient()
    print("   ✓ LLM 客户端已就绪")

    # ── Step 5: 模拟学生提交作业 ────────────────────
    print("\n📝 Step 5: 模拟学生提交作业...")
    # MVP：使用内置的示例作业文本，无需真实文件
    sample_homework_path = _create_sample_homework()
    submission = receive_file("123456", sample_homework_path, "C001", "A001")
    print(f"   ✓ 作业已归档：{submission.id}")
    print(f"   学生QQ：{submission.student_qq}")
    print(f"   迟交：{'是' if submission.is_late else '否'}")

    # ── Step 6: AI 批改 ────────────────────────────
    print("\n🔍 Step 6: AI 批改中（可能需要 30-60 秒）...")
    submission_text = get_submission_text(submission.id)
    result = grade(submission_text, rubric, SAMPLE_COURSEWARE, llm, submission.id)

    # 保存批改结果
    from storage.db import save_grading_result
    save_grading_result(submission.id, result)
    print("   ✓ 批改完成")

    # ── Step 7: 发送批改结果 ───────────────────────
    print("\n📨 Step 7: 批改结果通知：")
    print_result(result, "数据库原理", "第二次作业")

    # ── Step 8: 申诉流程（交互式）──────────────────
    print("\n⚖️ Step 8: 申诉流程")
    want_appeal = input("学生是否要申诉？（y/n）：").strip().lower()
    if want_appeal == "y":
        reason = input("请输入申诉理由：").strip()
        if not reason:
            reason = "我认为方案设计部分有考虑边界条件，详见作业第3段"

        # 提交申诉
        appeal_record = submit_appeal(submission.id, "123456", reason)
        print(f"\n   ✓ 申诉已提交，申诉ID：{appeal_record.id}")

        # AI 重评
        print("   🤖 AI 重新评分中...")
        regrade_result = trigger_regrade(appeal_record.id, rubric, SAMPLE_COURSEWARE, llm)
        print(f"   ✓ 重评完成：原分 {appeal_record.original_score} → {regrade_result.total_score}")

        # 显示重评结果
        print("\n   重评结果：")
        print_result(regrade_result, "数据库原理", "第二次作业（重评）")

        # 助教审核（交互式）
        print("\n👨‍🏫 助教审核环节：")
        resolve_appeal_interactive(appeal_record.id, regrade_result, appeal_record.original_score)

        # 显示最终结果
        print("\n" + get_appeal_summary(appeal_record.id))
    else:
        print("   不申诉，流程结束。")

    print("\n" + "=" * 60)
    print("  MVP 演示完成！")
    print("=" * 60)


def _create_sample_homework() -> str:
    """创建一份示例学生作业文件。"""
    from pathlib import Path
    sample_dir = Path(__file__).parent / "data" / "sample"
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample_path = sample_dir / "sample_homework.txt"

    content = """数据库第二次作业

一、问题分析

本次作业要求针对一个在线图书管理系统的数据库进行设计。通过分析，我识别出以下核心问题：

1. 系统需要管理大量的图书信息，包括书名、作者、出版社、ISBN、分类等
2. 用户可以借阅和归还图书，需要记录借阅历史
3. 系统需要支持多用户并发访问，需要考虑数据一致性
4. 需要实现图书检索功能，支持按书名、作者、分类等条件查询

我认为核心问题是如何在保证数据一致性的前提下，设计出高效的数据库结构。

二、方案设计

基于上述分析，我设计了以下数据库方案：

1. 图书表（Books）：存储图书基本信息
   - book_id (PK), title, author, publisher, ISBN, category, stock_count

2. 用户表（Users）：存储注册用户信息
   - user_id (PK), name, email, phone, registration_date

3. 借阅记录表（BorrowRecords）：记录借阅和归还
   - record_id (PK), user_id (FK), book_id (FK), borrow_date, due_date, return_date

4. 分类表（Categories）：图书分类
   - category_id (PK), name, parent_id

关系模式：
- Users 与 BorrowRecords 是一对多关系
- Books 与 BorrowRecords 是一对多关系
- Categories 是自引用关系（parent_id）

数据完整性约束：
- 实体完整性：所有主键不为空
- 参照完整性：外键必须引用已存在的记录
- 用户定义完整性：stock_count >= 0

三、表达规范

本方案遵循数据库设计的标准流程，从需求分析到概念设计再到逻辑设计。采用了E-R模型进行概念结构设计，并将其转换为关系模式。

参考文献：
- 《数据库系统概论》第3章 数据库设计
- 课件中关于约束条件的讨论
"""

    sample_path.write_text(content, encoding="utf-8")
    return str(sample_path)


if __name__ == "__main__":
    run_demo()
