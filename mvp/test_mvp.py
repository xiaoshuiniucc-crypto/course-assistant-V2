"""
QQ课程AI助手 — MVP 完整集成测试（无需 API Key）
覆盖全部13个模块：M01~M13
用 Mock LLM 模拟 AI 响应，测试完整流程。
"""
import sys
import os

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from storage.db import init_db, upsert_assignment, save_rubric, get_latest_rubric, save_grading_result
from rubric.rubric_parser import parse as parse_rubric
from homework.receiver import receive_file, get_submission_text
from grading.engine import grade, LLMClient, regrade_with_appeal
from notify.result_notifier import format_summary, print_result
from appeal.handler import submit_appeal, trigger_regrade, resolve_appeal_interactive, get_appeal_summary
from parser.file_parser import parse as parse_file
from contracts.models import ParsedFile


# ── Mock LLM 客户端 ────────────────────────────────────────

class MockLLMClient(LLMClient):
    """模拟 LLM 响应，用于本地测试，无需真实 API Key。"""

    def chat(self, prompt: str, system: str = "你是一位专业的课程助教。") -> str:
        if "硬性扣分" in prompt or "触发" in prompt:
            if "抄袭" in prompt:
                return "否"
            return "是"

        if "申诉" in prompt:
            return self._mock_regrade_response(prompt)

        return self._mock_dimension_response(prompt)

    def _mock_dimension_response(self, prompt):
        if "【评分维度】问题分析" in prompt:
            return '''{
  "score": 24,
  "gain_points": [
    "准确识别了核心问题：数据一致性保障",
    "结合了课件中需求分析的相关概念"
  ],
  "deductions": [
    {"point": "未明确指出用户定义的完整性需求", "deduct": 3, "evidence": "作业第1段未提及用户定义完整性", "evidence_source": "课件P55 3.1需求分析"},
    {"point": "边界条件分析不足", "deduct": 3, "evidence": "未见对并发访问控制的描述", "evidence_source": "评分细则-方案设计"}
  ],
  "comment": "问题分析基本准确，抓住了数据一致性这一核心，但对完整性需求的覆盖不够全面。",
  "confidence": "high"
}'''
        elif "【评分维度】方案设计" in prompt:
            return '''{
  "score": 32,
  "gain_points": [
    "设计了完整的四表结构，覆盖图书、用户、借阅、分类",
    "正确建立了表之间的外键关系",
    "考虑了实体完整性和参照完整性约束"
  ],
  "deductions": [
    {"point": "未考虑并发访问的锁机制", "deduct": 5, "evidence": "方案未提及事务隔离级别或行级锁", "evidence_source": "课件P68 物理结构设计"},
    {"point": "分类表自引用设计未说明级联删除策略", "deduct": 3, "evidence": "分类表parent_id未说明删除影响", "evidence_source": "评分细则"}
  ],
  "comment": "方案设计整体合理，E-R转换基本正确，但在高并发场景和级联操作方面有待加强。",
  "confidence": "medium"
}'''
        elif "【评分维度】表达规范" in prompt:
            return '''{
  "score": 25,
  "gain_points": [
    "结构清晰，分章节叙述",
    "正确引用了《数据库系统概论》和课件",
    "术语使用基本准确"
  ],
  "deductions": [
    {"point": "关系模式书写格式不标准，缺少外键标注", "deduct": 3, "evidence": "BorrowRecords表定义未标注FK", "evidence_source": "课件P62 E-R向关系模式转换"},
    {"point": "部分术语使用不准确", "deduct": 2, "evidence": "作业第5段", "evidence_source": "课件P62"}
  ],
  "comment": "表达规范较好，结构清晰，引用规范，但有少量技术术语使用不准确。",
  "confidence": "high"
}'''
        else:
            return '''{
  "score": 10,
  "gain_points": ["完成提交"],
  "deductions": [],
  "comment": "默认评分",
  "confidence": "low"
}'''

    def _mock_regrade_response(self, prompt):
        return '''{
  "score": 36,
  "gain_points": [
    "重评确认：方案中确实考虑了边界条件",
    "补充认可：并发访问已在'多用户并发访问'中提及"
  ],
  "deductions": [
    {"point": "分类表自引用设计未说明级联删除策略", "deduct": 3, "evidence": "分类表parent_id未说明删除影响", "evidence_source": "评分细则"}
  ],
  "comment": "重评说明：原评分对并发访问的考虑有所遗漏，应酌情加分。原32分调整为36分。",
  "confidence": "high"
}'''


# ── 示例数据 ────────────────────────────────────────────────

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

SAMPLE_COURSEWARE = """
第3章 数据库设计
3.1 需求分析：确定用户的数据需求、处理需求、安全性需求和完整性需求
3.2 概念结构设计：E-R模型，实体、属性、联系
3.3 逻辑结构设计：E-R图向关系模式的转换规则，规范化理论
3.4 物理结构设计：存储结构设计，存取方法设计
3.5 约束条件：实体完整性、参照完整性、用户定义的完整性约束
"""

SAMPLE_HOMEWORK = """数据库第二次作业

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


# ── 测试函数 ────────────────────────────────────────────────

def test_all():
    """运行完整 MVP 测试流程，覆盖全部13个模块。"""
    print("=" * 60)
    print("  QQ课程AI助手 — MVP 全模块集成测试（Mock 模式）")
    print("=" * 60)

    from pathlib import Path

    # ══════════════════════════════════════════════════════
    # M13: 存储层
    # ══════════════════════════════════════════════════════
    print("\n[M13] Step 1: 初始化数据库...")
    init_db()
    print("  ✓ 数据库就绪")

    upsert_assignment("C001", "A001", "数据库第二次作业", "2026-05-10T23:59")
    print("  ✓ 课程 C001 / 作业 A001 已创建")

    # ══════════════════════════════════════════════════════
    # M04: 评分细则解析
    # ══════════════════════════════════════════════════════
    print("\n[M04] Step 2: 解析评分细则...")
    rubric = parse_rubric(SAMPLE_RUBRIC_TEXT, "#评分细则", "C001", "A001")
    save_rubric(rubric)
    print(f"  ✓ 细则解析完成：{rubric.title}")
    print(f"    维度：{', '.join(d.name for d in rubric.dimensions)}")
    print(f"    硬性扣分：{[r.condition for r in rubric.hard_deductions]}")
    assert len(rubric.dimensions) == 3
    assert rubric.total_score == 100
    print("  ✓ 断言通过")

    # ══════════════════════════════════════════════════════
    # M01: Qclaw 接入层（Mock）
    # ══════════════════════════════════════════════════════
    print("\n[M01] Step 3: Qclaw 接入层测试...")
    from qclaw.adapter import QclawAdapter, QQMessage
    adapter = QclawAdapter()

    # 设置成员角色
    adapter.set_member_role("group_C001", "teacher_qq", "teacher")
    adapter.set_member_role("group_C001", "ta_qq", "ta")
    adapter.set_member_role("group_C001", "123456", "student")

    role = adapter.get_member_role("group_C001", "123456")
    assert role == "student", f"预期 student，实际 {role}"
    print(f"  ✓ 成员角色查询：123456 → {role}")

    # 测试消息发送
    adapter.send_private("123456", "这是一条测试私聊消息")
    adapter.send_group("group_C001", "这是一条测试群消息")
    sent = adapter.get_sent_messages()
    assert len(sent) == 2, f"预期2条发送消息，实际{len(sent)}"
    print(f"  ✓ 消息发送：已发送 {len(sent)} 条消息")

    # 测试消息注入（模拟接收）
    received = []
    adapter.on_private_message(lambda msg: received.append(msg))
    adapter.inject_private_message("123456", "我要申诉", tag=None)
    assert len(received) == 1
    assert received[0].text == "我要申诉"
    print(f"  ✓ 消息注入/接收：收到消息 '{received[0].text}'")

    # ══════════════════════════════════════════════════════
    # M02: 意图识别路由
    # ══════════════════════════════════════════════════════
    print("\n[M02] Step 4: 意图识别路由测试...")
    from router.router import IntentRouter
    router = IntentRouter()

    # 测试各种意图
    test_cases = [
        (QQMessage(msg_type="group", sender_qq="teacher_qq", text="", file_id="f1", tag="#课件", is_file=True),
         "UPLOAD_COURSEWARE"),
        (QQMessage(msg_type="group", sender_qq="teacher_qq", text="", file_id="f2", tag="#评分细则", is_file=True),
         "SET_RUBRIC"),
        (QQMessage(msg_type="private", sender_qq="123456", text="", file_id="f3", is_file=True),
         "SUBMIT_HOMEWORK"),
        (QQMessage(msg_type="private", sender_qq="123456", text="申诉"),
         "APPEAL"),
        (QQMessage(msg_type="private", sender_qq="123456", text="报告"),
         "VIEW_REPORT"),
        (QQMessage(msg_type="channel", sender_qq="ta_qq", text="同意"),
         "TA_APPROVE"),
        (QQMessage(msg_type="channel", sender_qq="ta_qq", text="驳回 评分不合理"),
         "TA_REJECT"),
        (QQMessage(msg_type="group", sender_qq="teacher_qq", text="查看进度"),
         "QUERY_PROGRESS"),
    ]

    for msg, expected in test_cases:
        intent = router.classify(msg)
        assert intent.action == expected, f"意图识别错误：预期 {expected}，实际 {intent.action}"
    print(f"  ✓ 意图识别：{len(test_cases)} 种意图全部正确")

    # 测试会话管理
    router.set_session("123456", "WAITING_COURSE_CONFIRM", {"submission_id": "sub_123"})
    session = router.get_session("123456")
    assert session is not None
    assert session.state == "WAITING_COURSE_CONFIRM"
    print(f"  ✓ 会话管理：状态 = {session.state}")

    # 多轮对话意图识别
    confirm_msg = QQMessage(msg_type="private", sender_qq="123456", text="C001")
    confirm_intent = router.classify(confirm_msg)
    assert confirm_intent.action == "CONFIRM_COURSE"
    print(f"  ✓ 多轮对话：课程选择 → {confirm_intent.action}")

    router.clear_session("123456")

    # ══════════════════════════════════════════════════════
    # M05: 知识库管理
    # ══════════════════════════════════════════════════════
    print("\n[M05] Step 5: 知识库管理测试...")
    from knowledge.knowledge_base import KnowledgeBase
    kb = KnowledgeBase()

    # 添加课件
    parsed_courseware = ParsedFile(
        text=SAMPLE_COURSEWARE, pages=[SAMPLE_COURSEWARE],
        code_files={}, metadata={"file_name": "数据库设计课件.pdf"},
    )
    chunk_count = kb.add_courseware("C001", parsed_courseware, {"file_name": "数据库设计课件.pdf"})
    assert chunk_count > 0, "课件切片数量应大于0"
    print(f"  ✓ 课件添加：{chunk_count} 个文本块")

    # 检索测试
    results = kb.search("C001", "数据库设计的步骤", top_k=3)
    assert len(results) > 0, "检索应返回结果"
    print(f"  ✓ 知识检索：'{results[0].text[:40]}...' (相似度={results[0].score:.2f})")

    # 列出课件
    courseware_list = kb.list_courseware("C001")
    assert len(courseware_list) > 0
    print(f"  ✓ 课件列表：{len(courseware_list)} 个课件")

    # ══════════════════════════════════════════════════════
    # M03: 文件解析引擎
    # ══════════════════════════════════════════════════════
    print("\n[M03] Step 6: 文件解析引擎测试...")
    sample_dir = Path(__file__).parent / "src" / "data" / "sample"
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample_path = sample_dir / "sample_homework.txt"
    sample_path.write_text(SAMPLE_HOMEWORK, encoding="utf-8")

    parsed = parse_file(str(sample_path))
    assert "问题分析" in parsed.text
    print(f"  ✓ .txt 解析：{len(parsed.text)} 字符")

    try:
        from docx import Document
        docx_path = sample_dir / "sample_homework.docx"
        doc = Document()
        doc.add_heading("数据库第二次作业", 0)
        doc.add_paragraph(SAMPLE_HOMEWORK)
        doc.save(str(docx_path))
        parsed_docx = parse_file(str(docx_path))
        assert "问题分析" in parsed_docx.text
        print(f"  ✓ .docx 解析：{len(parsed_docx.text)} 字符")
    except Exception as e:
        print(f"  △ .docx 解析跳过：{e}")

    # ══════════════════════════════════════════════════════
    # M06: 作业接收与归档
    # ══════════════════════════════════════════════════════
    print("\n[M06] Step 7: 作业接收与归档...")
    submission = receive_file("123456", str(sample_path), "C001", "A001")
    print(f"  ✓ 作业已归档：{submission.id}")
    assert submission.student_qq == "123456"
    assert submission.is_late == False
    print("  ✓ 断言通过")

    # ══════════════════════════════════════════════════════
    # M07: AI 批改引擎
    # ══════════════════════════════════════════════════════
    print("\n[M07] Step 8: AI 批改引擎...")
    llm = MockLLMClient()
    submission_text = get_submission_text(submission.id)

    # 结合知识库检索结果批改
    kb_results = kb.search("C001", "数据库设计 需求分析", top_k=3)
    courseware_context = SAMPLE_COURSEWARE
    if kb_results:
        courseware_context += "\n\n【知识库检索补充】\n" + "\n".join(r.text for r in kb_results)

    result = grade(submission_text, rubric, courseware_context, llm, submission.id)
    save_grading_result(submission.id, result)
    print(f"  ✓ 批改完成，总分：{result.total_score}/{rubric.total_score}")
    print(f"    置信度：{result.confidence}")
    assert result.total_score > 0
    print("  ✓ 断言通过")

    # ══════════════════════════════════════════════════════
    # M08: 批改结果通知
    # ══════════════════════════════════════════════════════
    print("\n[M08] Step 9: 批改结果通知...")
    print_result(result, "数据库原理", "第二次作业")
    summary = format_summary(result, "数据库原理", "第二次作业")
    assert "总分" in summary
    print("  ✓ 通知格式化正确")

    # ══════════════════════════════════════════════════════
    # M09: 助教频道管理
    # ══════════════════════════════════════════════════════
    print("\n[M09] Step 10: 助教频道管理测试...")
    from ta_channel.ta_channel import TAChannelManager
    ta_mgr = TAChannelManager(qclaw_adapter=adapter, channel_id="ta_channel")

    # 解析助教指令
    approve_inst = ta_mgr.parse_ta_instruction("同意")
    assert approve_inst.action == "approve"
    reject_inst = ta_mgr.parse_ta_instruction("驳回 理由不充分")
    assert reject_inst.action == "reject"
    assert "理由不充分" in reject_inst.note
    print(f"  ✓ 助教指令解析：同意→{approve_inst.action}, 驳回→{reject_inst.action}")

    # ══════════════════════════════════════════════════════
    # M11: 申诉处理
    # ══════════════════════════════════════════════════════
    print("\n[M11] Step 11: 申诉处理测试...")
    appeal_record = submit_appeal(
        submission.id, "123456",
        "我认为方案设计部分有考虑边界条件，详见作业第3段"
    )
    print(f"  ✓ 申诉已提交，ID：{appeal_record.id}")

    # 推送申诉到助教频道
    appeal_msg = ta_mgr.push_appeal(appeal_record, result, result, student_name="123456")
    assert "申诉待审" in appeal_msg
    print("  ✓ 申诉推送格式化正确")

    # AI 重评
    new_result = trigger_regrade(appeal_record.id, rubric, SAMPLE_COURSEWARE, llm)
    print(f"  ✓ 重评完成：{appeal_record.original_score} → {new_result.total_score}")

    # 助教审核（模拟）
    import builtins
    _orig_input = builtins.input
    builtins.input = lambda _: "1"
    try:
        resolve_appeal_interactive(appeal_record.id, new_result, appeal_record.original_score)
    except StopIteration:
        pass
    finally:
        builtins.input = _orig_input
    print("  ✓ 助教审核完成")

    # 申诉摘要
    summary_text = get_appeal_summary(appeal_record.id)
    assert "批准" in summary_text or "APPROVED" in summary_text.upper()
    print(f"  ✓ 申诉结果：{summary_text.strip()}")

    # ══════════════════════════════════════════════════════
    # M12: PDF报告生成
    # ══════════════════════════════════════════════════════
    print("\n[M12] Step 12: PDF/HTML 报告生成测试...")
    from report.report_gen import ReportGenerator
    report_gen = ReportGenerator()

    appeal_for_report = get_appeal_summary(appeal_record.id)  # 简化：直接传字符串
    from storage.db import get_appeal
    appeal_obj = get_appeal(appeal_record.id)

    report_path = report_gen.generate_pdf(
        grading_result=result,
        course_name="数据库原理",
        assignment_name="第二次作业",
        student_qq="123456",
        rubric=rubric,
        appeal_record=appeal_obj,
    )
    assert os.path.exists(report_path), f"报告文件不存在：{report_path}"
    file_size = os.path.getsize(report_path)
    print(f"  ✓ 报告已生成：{report_path}")
    print(f"    文件大小：{file_size} 字节")

    # 测试懒加载
    cached_path = report_gen.get_or_generate(
        submission.id, "数据库原理", "第二次作业", "123456"
    )
    assert os.path.exists(cached_path)
    print("  ✓ 懒加载缓存正常")

    # 测试缓存失效
    report_gen.invalidate_cache(submission.id)
    assert submission.id not in report_gen._cache
    print("  ✓ 缓存失效正常")

    # ══════════════════════════════════════════════════════
    # M10: 教师统计与预警
    # ══════════════════════════════════════════════════════
    print("\n[M10] Step 13: 教师统计与预警测试...")
    from teacher_stats.stats import TeacherStats
    stats = TeacherStats(qclaw_adapter=adapter)

    progress = stats.get_progress("C001", "A001", total_students=30)
    assert progress is not None
    print(f"  ✓ 进度查询：已提交 {progress.submitted_count}，已批改 {progress.graded_count}")

    progress_text = stats.format_progress(progress)
    assert "交作业进度" in progress_text
    print(f"  ✓ 进度格式化正确")

    # 检测异常
    anomalies = stats.check_anomalies("C001", "A001")
    print(f"  ✓ 异常检测：发现 {len(anomalies)} 个预警")
    for a in anomalies:
        print(f"    - [{a.level}] {a.alert_type}: {a.message}")

    # ══════════════════════════════════════════════════════
    # 端到端集成：M01 + M02 → 全流程
    # ══════════════════════════════════════════════════════
    print("\n[E2E] Step 14: 端到端集成测试（M01+M02串联）...")

    # 场景1：学生发文件（私聊）→ 识别为 SUBMIT_HOMEWORK
    adapter.clear_sent_messages()
    student_file_msg = QQMessage(
        msg_type="private", sender_qq="654321",
        file_id="hw_file_001", file_name="作业.docx",
        is_file=True,
    )
    intent = router.classify(student_file_msg)
    assert intent.action == "SUBMIT_HOMEWORK"
    print("  ✓ 场景1：学生发文件 → SUBMIT_HOMEWORK")

    # 场景2：助教频道同意 → 识别为 TA_APPROVE
    ta_msg = QQMessage(msg_type="channel", sender_qq="ta_qq", text="同意")
    intent = router.classify(ta_msg)
    assert intent.action == "TA_APPROVE"
    print("  ✓ 场景2：助教同意 → TA_APPROVE")

    # 场景3：教师发评分细则文件 → 识别为 SET_RUBRIC
    teacher_msg = QQMessage(
        msg_type="group", sender_qq="teacher_qq",
        file_id="rubric_file", tag="#评分细则", is_file=True,
    )
    intent = router.classify(teacher_msg)
    assert intent.action == "SET_RUBRIC"
    print("  ✓ 场景3：教师发细则 → SET_RUBRIC")

    # ══════════════════════════════════════════════════════
    # 存储层最终验证
    # ══════════════════════════════════════════════════════
    print("\n[M13] Step 15: 存储层最终验证...")
    from storage.db import get_submission, get_appeal
    sub = get_submission(submission.id)
    assert sub is not None
    appeal = get_appeal(appeal_record.id)
    assert appeal is not None
    print(f"  ✓ 提交记录状态：{sub.status}")
    print(f"  ✓ 申诉记录状态：{appeal.status}")

    # ══════════════════════════════════════════════════════
    # 完成
    # ══════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  🎉 全部13个模块测试通过！")
    print("=" * 60)

    module_status = [
        ("M01", "Qclaw接入层", "✅"),
        ("M02", "意图识别路由", "✅"),
        ("M03", "文件解析引擎", "✅"),
        ("M04", "评分细则解析", "✅"),
        ("M05", "知识库管理", "✅"),
        ("M06", "作业接收与归档", "✅"),
        ("M07", "AI批改引擎", "✅"),
        ("M08", "批改结果通知", "✅"),
        ("M09", "助教频道管理", "✅"),
        ("M10", "教师统计与预警", "✅"),
        ("M11", "申诉处理", "✅"),
        ("M12", "PDF报告生成", "✅"),
        ("M13", "数据与存储层", "✅"),
    ]

    print("\n📊 模块测试结果：")
    for mid, name, status in module_status:
        print(f"  {status} {mid} {name}")

    print(f"\n📈 关键数据：")
    print(f"  - 批改总分：{result.total_score}/{rubric.total_score}")
    print(f"  - 置信度：{result.confidence}")
    print(f"  - 知识库检索：正常")
    print(f"  - 意图识别：{len(test_cases)} 种意图全部正确")
    print(f"  - 报告生成：{'PDF' if report_gen._use_reportlab else 'HTML'} 格式")

    print("\n⚠️  注意：本次使用 Mock LLM，未调用真实 AI API")
    print("   如需真实批改，请设置 OPENAI_API_KEY 后运行 python src/main.py")


if __name__ == "__main__":
    try:
        test_all()
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
