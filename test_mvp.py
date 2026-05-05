"""
MVP 全模块集成测试
覆盖全部 13 个模块（M01-M13）
"""
from __future__ import annotations
import os
import sys
import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ── 添加 src 到路径 ────────────────────
SRC = Path(__file__).parent / "src"
sys.path.insert(0, str(SRC))

from contracts.models import (
    IntentType, MessageType, SubmitStatus,
    RubricDimension, QQMessage, IntentResult,
)
from storage.db import DB
from qclaw.adapter import QclawAdapter
from router.router import IntentRouter, SessionState
from knowledge.knowledge_base import KnowledgeBase
from ta_channel.ta_channel import TAChannelManager
from teacher_stats.stats import TeacherStats
from report.report_gen import ReportGenerator
from rubric.rubric_parser import RubricParser
from fileparser.file_parser import FileParser
from homework.receiver import HomeworkReceiver
from grading.engine import GradingEngine
from notify.result_notifier import ResultNotifier
from appeal.handler import AppealHandler


PASS = 0
FAIL = 0
LOG = []

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def log(section: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    tag = "✅" if ok else "❌"
    line = f"  {tag} [{section}] {detail}"
    LOG.append(line)
    if ok:
        PASS += 1
        print(line.replace("[E2E] ", ""))
    else:
        FAIL += 1
        print(line)


async def main():
    print("=" * 60)
    print(" QQ 课程助手 MVP — 全模块集成测试")
    print("=" * 60)

    DB_PATH = "test_mvp.db"
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    # ── 初始化所有模块 ─────────────────
    print("\n[初始化] 模块初始化...")
    db = DB(DB_PATH)
    adapter = QclawAdapter()
    router = IntentRouter(db)
    kb = KnowledgeBase(db)
    ta_ch = TAChannelManager(db)
    stats = TeacherStats(db)
    report = ReportGenerator(db, output_dir="test_reports")
    rubric_parser = RubricParser()
    file_parser = FileParser()
    homework = HomeworkReceiver(db)
    engine = GradingEngine(db)
    notifier = ResultNotifier(adapter)
    appeal_h = AppealHandler(db, engine)
    print("  ✅ 所有模块初始化完成\n")

    # ── M01: Qclaw 适配器 ────────────────
    print("─" * 60)
    print(" M01: Qclaw 适配器")
    print("─" * 60)

    # Mock 消息创建
    msg = adapter.create_mock_message(
        user_id="U1001", content="你好，我想提交作业", message_type=MessageType.PRIVATE
    )
    log("M01", msg.user_id == "U1001", "Mock 消息创建")

    # 注入消息处理
    received = []
    async def _handler(m: QQMessage):
        received.append(m)
    adapter.on_message(_handler)
    adapter.inject_message(msg)
    await adapter.process_injected()
    log("M01", len(received) == 1, "消息注入与分发")
    log("M01", adapter.mode.startswith("mock") or adapter.mode.startswith("real"),
         f"适配器模式: {adapter.mode}")

    # ── M02: 意图路由器 ────────────────
    print("\n[模块] M02: 意图路由器")
    print("─" * 60)

    # 测试各个意图
    test_intents = [
        ("上传课件 初识Access数据库", IntentType.UPLOAD_COURSEWARE),
        ("设置评分标准：内容40%逻辑30%表达30%", IntentType.SET_RUBRIC),
        ("布置作业：Access实验报告", IntentType.SET_ASSIGNMENT),
        ("提交作业", IntentType.SUBMIT_HOMEWORK),
        ("申诉：评分不合理", IntentType.APPEAL),
        ("查看报告", IntentType.VIEW_REPORT),
        ("同意申诉，加回分数", IntentType.TA_APPROVE),
        ("驳回申诉", IntentType.TA_REJECT),
        ("查询进度", IntentType.QUERY_PROGRESS),
        ("请教一下数据库", IntentType.ASK_QUESTION),
    ]

    for content, expected_intent in test_intents:
        msg = QQMessage(
            user_id="U1001", group_id=None,
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            content=content, timestamp=datetime.now(),
            message_type=MessageType.PRIVATE,
        )
        result = router.classify(msg)
        ok = result.intent == expected_intent
        log("M02", ok, f"「{content[:15]}...」→ {result.intent} {'✓' if ok else '✗ (got '+result.intent+')'}")

    # 测试多轮对话
    msg2 = QQMessage(
        user_id="U2001", group_id=None,
        message_id="msg_multi_1", content="申诉",
        timestamp=datetime.now(), message_type=MessageType.PRIVATE,
    )
    result2 = router.classify(msg2)
    log("M02", result2.intent == IntentType.APPEAL,
         f"申诉意图识别 intent={result2.intent}")

    # ── M03: 知识库 (ChromaDB) ─────────
    print("\n[模块] M03: 知识库 (ChromaDB)")
    print("─" * 60)

    SAMPLE_TEXT = (
        "第一章 Access数据库基础\n\n"
        "Access是微软Office套件中的数据库管理软件。"
        "它提供了表、查询、窗体、报表等对象。\n\n"
        "第二章 SQL查询基础\n\n"
        "SQL是结构化查询语言，用于操作关系型数据库。"
        "SELECT语句用于查询数据。"
    )
    kb.add_courseware("CW001", SAMPLE_TEXT)
    count = kb.get_chunk_count("CW001")
    log("M03", count > 0, f"ChromaDB分块入库: {count} 块")

    # 检索测试
    results = kb.search("Access是什么", top_k=2)
    log("M03", len(results) > 0, f"向量检索返回 {len(results)} 条")

    # 短文本快路径
    short_text = "Access是数据库软件"
    kb.add_courseware("CW002", short_text)
    log("M03", kb.get_chunk_count("CW002") == 1, "短文本单块策略")

    # 滑动窗口分块（需要超过2000字符才触发）
    long_text = "章节一" * 800  # 2400字符，超过2000阈值
    chunks = kb._chunk(long_text)
    log("M03", len(chunks) > 1, f"滑动窗口分块: {len(chunks)} 块")

    # ── M04: 评分标准解析 ───────────────
    print("\n[模块] M04: 评分标准解析")
    print("─" * 60)

    rubric_text = (
        "内容准确性:0.4:答案是否正确\n"
        "逻辑性:0.3:推理是否合理\n"
        "表达:0.3:语言是否清晰\n"
        "硬扣分:迟到扣10分"
    )
    rubric = rubric_parser.parse(rubric_text, title="Access作业评分标准")
    log("M04", len(rubric.dimensions) == 3, f"评分维度数: {len(rubric.dimensions)}")
    log("M04", rubric.hard_rules == ["迟到扣10分"], "硬扣分规则解析")
    db.save_rubric(rubric)
    loaded = db.get_rubric(rubric.id)
    log("M04", loaded is not None, "评分标准持久化")
    log("M04", abs(loaded.calc_total_weight() - 1.0) < 0.01, "权重归一化正确")

    # ── M05: 作业布置与提交 ───────────
    print("\n[模块] M05: 作业布置与提交")
    print("─" * 60)

    assignment = type(
        "A", (), {
            "id": "ASGN001", "title": "Access实验报告",
            "description": "完成Access数据库设计",
            "rubric_id": rubric.id, "courseware_id": "CW001",
            "deadline": datetime.now() + timedelta(days=7),
            "max_score": 100.0, "created_at": datetime.now(),
        }
    )()
    db.save_assignment(assignment)
    log("M05", db.get_assignment("ASGN001") is not None, "作业创建")

    sub = homework.receive(
        assignment_id="ASGN001", student_id="U1001",
        student_name="张三", content="我的Access报告内容..."
    )
    log("M05", sub.id.startswith("sub_"), f"作业提交: {sub.id}")
    log("M05", sub.status == SubmitStatus.SUBMITTED, "提交状态正确")

    # ── M06: AI 批改引擎 ────────────────
    print("\n[模块] M06: AI 批改引擎")
    print("─" * 60)

    result = engine.grade(sub, rubric, "Access实验报告")
    log("M06", result.total_score > 0, f"批改完成: {result.total_score}分")
    log("M06", len(result.dimension_scores) > 0, "维度评分已生成")
    log("M06", 0 <= result.confidence <= 1, f"置信度: {result.confidence}")
    log("M06", sub.score is not None, f"提交已更新分数: {sub.score}")

    # 批量批改
    for i, name in enumerate(["李四", "王五"]):
        s = homework.receive(
            "ASGN001", f"U100{i+2}", name,
            f"{name}的Access报告内容..." * 10
        )
        r = engine.grade(s, rubric)
        log("M06", r.total_score > 0, f"批量批改 {name}: {r.total_score}分")

    # ── M07: 结果通知 ────────────────
    print("\n[模块] M07: 结果通知")
    print("─" * 60)

    notif = await notifier.notify_student(sub, result, rubric)
    log("M07", "总分" in notif, "通知文本生成")
    log("M07", "维度评分" in notif or "维度" in notif, "通知含维度详情")

    summary = notifier.format_batch_summary(
        [db.get_grading_result(sub.id) for sub in db.list_submissions("ASGN001") if db.get_grading_result(sub.id)]
    )
    log("M07", "批改完成摘要" in summary, "批量摘要生成")

    # ── M08: 申诉处理 ────────────────
    print("\n[模块] M08: 申诉处理")
    print("─" * 60)

    appeal = appeal_h.submit_appeal(sub.id, "U1001", "评分偏低，请求复查")
    log("M08", appeal.status == "pending", f"申诉创建: {appeal.id}")
    # 从 DB 重新加载 submission 以获取最新 appeal_status
    sub_fresh = db.get_submission(sub.id)
    log("M08", sub_fresh.appeal_status == "pending", "提交状态更新为pending")

    # TA 批准申诉（重批）
    appeal2 = appeal_h.process_appeal(appeal.id, approved=True, reviewer_id="TA01")
    log("M08", appeal2.status == "approved", "申诉批准并重批")
    new_result = db.get_grading_result(sub.id)
    log("M08", new_result is not None, "重批结果已保存")
    if new_result:
        log("M08", new_result.appeal_count >= 1, f"申诉次数: {new_result.appeal_count}")

    # ── M09: TA 渠道管理 ────────────────
    print("\n[模块] M09: TA 渠道管理")
    print("─" * 60)

    push_msg = ta_ch.format_appeal_push(appeal2, sub)
    log("M09", "申诉请求" in push_msg, "TA推送格式化")

    action, note = ta_ch.parse_ta_instruction("同意，加回分数")
    log("M09", action == "approve", f"指令解析: {action}")

    action2, _ = ta_ch.parse_ta_instruction("驳回")
    log("M09", action2 == "reject", f"驳回指令解析: {action2}")

    confirm = ta_ch.confirm_decision(appeal2.id, "approve")
    log("M09", "已批准" in confirm, "确认信息格式化")

    summary_ta = ta_ch.get_pending_appeals_summary()
    log("M09", isinstance(summary_ta, str), "待处理申诉摘要")

    # ── M10: 教师统计 ────────────────
    print("\n[模块] M10: 教师统计 & 异常检测")
    print("─" * 60)

    progress = stats.get_assignment_progress("ASGN001")
    log("M10", progress["total_submissions"] >= 3, f"进度统计: {progress['total_submissions']} 份")

    student_prog = stats.get_student_progress("ASGN001")
    log("M10", len(student_prog) >= 3, f"学生进度: {len(student_prog)} 人")

    alerts = stats.detect_anomalies("ASGN001")
    log("M10", isinstance(alerts, list), f"异常检测: {len(alerts)} 条预警")

    report_text = stats.format_progress_report("ASGN001")
    log("M10", "进度报告" in report_text, "教师报告格式化")

    # ── M11: 报告生成 ────────────────
    print("\n[模块] M11: 报告生成 (reportlab PDF / HTML)")
    print("─" * 60)

    path = report.get_or_generate("ASGN001")
    log("M11", path is not None, f"报告生成: {os.path.basename(path)}")
    log("M11", os.path.exists(path), f"文件存在: {path}")

    # 测试缓存（二次调用应命中缓存）
    path2 = report.get_or_generate("ASGN001")
    log("M11", path == path2, "缓存机制正常工作")
    report.invalidate("ASGN001")
    path3 = report.get_or_generate("ASGN001", force=True)
    log("M11", path3 is not None, "缓存失效后重新生成")

    # ── M12: 端到端流程测试 ───────────
    print("\n[模块] M12: 端到端流程 (M01消息 → M02意图 → 全链路)")
    print("─" * 60)

    # 模拟完整流程: 消息 → 意图 → 布置作业 → 提交 → 批改 → 报告
    e2e_msgs = [
        ("老师001", "布置作业：期末报告"),
        ("老师001", "设置评分标准：内容0.5表达0.5"),
        ("学生001", "提交作业：这是我的期末报告..."),
        ("学生001", "申诉：分数太低"),
    ]

    e2e_ok = True
    for user, content in e2e_msgs:
        msg = QQMessage(
            user_id=user, group_id=None,
            message_id=f"e2e_{uuid.uuid4().hex[:8]}",
            content=content, timestamp=datetime.now(),
            message_type=MessageType.PRIVATE,
        )
        # 使用路由器分类
        intent = router.classify(msg)
        if intent.intent == IntentType.UNKNOWN:
            e2e_ok = False
            log("E2E", False, f"消息识别失败: {content[:20]}")
            break

    # 模拟 TA 审批：先设置会话为等待 TA 审批状态
    ta_session_id = f"sess_TA001"
    router.set_waiting(
        session_id=ta_session_id,
        user_id="TA001",
        waiting_for="waiting_ta_review",
        intent=IntentType.TA_APPROVE,
    )
    ta_msg = QQMessage(
        user_id="TA001", group_id=None,
        message_id=f"e2e_ta_{uuid.uuid4().hex[:8]}",
        content="同意申诉", timestamp=datetime.now(),
        message_type=MessageType.PRIVATE,
    )
    ta_intent = router.classify(ta_msg)
    if ta_intent.intent == IntentType.UNKNOWN:
        e2e_ok = False
        log("E2E", False, f"TA消息识别失败: {ta_msg.content[:20]}")
    log("E2E", e2e_ok, "端到端意图识别链路")

    # ── M13: 存储层完整性 ────────────
    print("\n[模块] M13: 存储层完整性检查")
    print("─" * 60)

    # 检查各表数据
    tables = ["courseware", "rubrics", "assignments", "submissions",
              "grading_results", "appeals", "knowledge_chunks", "sessions"]
    for t in tables:
        try:
            cnt = db._conn.execute(f"SELECT COUNT(*) AS cnt FROM {t}").fetchone()["cnt"]
            log("M13", True, f"表 {t}: {cnt} 行")
        except Exception as e:
            log("M13", False, f"表 {t} 访问失败: {e}")

    # ── 测试结果汇总 ────────────────
    print("\n" + "=" * 60)
    print(f" 测试结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 60)

    if LOG:
        report_path = "test_report.log"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
        print(f"\n详细日志已保存: {report_path}")

    # 清理
    try:
        os.remove(DB_PATH)
        import shutil
        if os.path.exists("test_reports"):
            shutil.rmtree("test_reports")
    except Exception:
        pass

    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
