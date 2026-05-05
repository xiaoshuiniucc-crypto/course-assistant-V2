"""
M09 助教频道管理 — 完善功能专项测试
覆盖: TA 工作台指令、通知队列、申诉分配、审批历史
"""
from __future__ import annotations
import os
import sys
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

# ── 添加 src 到路径 ────────────────────
SRC = Path(__file__).parent / "src"
sys.path.insert(0, str(SRC))

from contracts.models import (
    IntentType, MessageType, SubmitStatus,
    RubricDimension, QQMessage, Appeal, HomeworkSubmission,
    Rubric, Assignment, GradingResult,
)
from storage.db import DB
from ta_channel.ta_channel import (
    TAChannelManager, TACommandParser, TACommand,
    NotifyQueue, NotifyPriority, NotifyItem,
)
from rubric.rubric_parser import RubricParser
from homework.receiver import HomeworkReceiver
from grading.engine import GradingEngine
from appeal.handler import AppealHandler


PASS = 0
FAIL = 0
LOG = []


def log(section: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    tag = "✅" if ok else "❌"
    line = f"  {tag} [{section}] {detail}"
    LOG.append(line)
    if ok:
        PASS += 1
        print(line)
    else:
        FAIL += 1
        print(line)


async def main():
    global PASS, FAIL
    print("=" * 60)
    print(" M09 助教频道管理 — 完善功能专项测试")
    print("=" * 60)

    DB_PATH = os.path.join(tempfile.mkdtemp(), "test_m09_full.db")
    db = DB(DB_PATH)
    ta = TAChannelManager(db)

    # ── 准备测试数据 ──────────────────
    rubric_parser = RubricParser()
    rubric = rubric_parser.parse(
        "内容准确性:0.4:答案是否正确\n逻辑性:0.3:推理是否合理\n表达:0.3:语言是否清晰"
    )
    rubric.id = "rubric_test"
    db.save_rubric(rubric)

    assignment = Assignment(
        id="asgn_test",
        title="测试作业",
        description="测试用",
        rubric_id=rubric.id,
        courseware_id=None,
        deadline=None,
    )
    db.save_assignment(assignment)

    homework = HomeworkReceiver(db)
    sub1 = homework.receive("asgn_test", "student_001", "张三", "我的作业内容很长，包含了很多有价值的分析。")
    sub2 = homework.receive("asgn_test", "student_002", "李四", "我的作业内容一般。")

    # 批改
    engine = GradingEngine(db)
    result1 = engine.grade(sub1, rubric, "测试作业")
    result2 = engine.grade(sub2, rubric, "测试作业")

    # ── T1: TA 指令解析 ─────────────────
    print("\n[T1] TA 工作台指令解析")
    print("─" * 60)

    # 指令格式
    test_commands = [
        ("/待审列表", TACommand.PENDING, {}),
        ("/pending", TACommand.PENDING, {}),
        ("/处理 appeal_001", TACommand.HANDLE, {"appeal_id": "appeal_001"}),
        ("/同意 appeal_001 分数偏低", TACommand.APPROVE, {"appeal_id": "appeal_001", "note": "分数偏低"}),
        ("/驳回 appeal_002 理由不成立", TACommand.REJECT, {"appeal_id": "appeal_002", "note": "理由不成立"}),
        ("/历史", TACommand.HISTORY, {}),
        ("/help", TACommand.HELP, {}),
        ("随机文本", "unknown", {}),
    ]

    for text, expected_cmd, expected_params in test_commands:
        cmd, params = TACommandParser.parse(text)
        ok = cmd == expected_cmd
        if expected_params:
            for k, v in expected_params.items():
                ok = ok and params.get(k) == v
        log("T1-指令", ok, f"\"{text}\" → cmd={cmd}, params={params}")

    # 快捷回复（兼容旧版）
    test_quick = [
        ("同意申诉", "approve"),
        ("同意，加回分数", "approve"),
        ("1", "approve"),
        ("驳回", "reject"),
        ("驳回，理由不充分", "reject"),
        ("2", "reject"),
    ]

    for text, expected_action in test_quick:
        action, note = ta.parse_ta_instruction(text)
        ok = action == expected_action
        log("T1-快捷", ok, f"\"{text}\" → action={action}")

    # is_ta_command
    log("T1", TACommandParser.is_ta_command("/待审列表"), "is_ta_command 正确识别")
    log("T1", not TACommandParser.is_ta_command("你好"), "is_ta_command 排除非指令")

    # 帮助文本
    help_text = TACommandParser.get_help_text()
    log("T1", len(help_text) > 100, f"帮助文本长度: {len(help_text)}")

    # ── T2: 通知队列 ─────────────────
    print("\n[T2] 通知队列")
    print("─" * 60)

    queue = NotifyQueue(db)
    log("T2", queue.size == 0, "初始队列为空")

    # 入队
    item1 = queue.enqueue("user_001", "普通通知", NotifyPriority.NORMAL)
    item2 = queue.enqueue("user_002", "紧急申诉", NotifyPriority.HIGH)
    item3 = queue.enqueue("user_003", "低优先级", NotifyPriority.LOW)
    log("T2", queue.size == 3, f"入队后大小: {queue.size}")

    # 优先级排序
    peek = queue.peek()
    log("T2", peek.priority == NotifyPriority.HIGH, f"最高优先级在前: {peek.priority}")

    # 出队
    item = queue.dequeue()
    log("T2", item.content == "紧急申诉", "出队优先级最高项")
    log("T2", queue.size == 2, f"出队后大小: {queue.size}")

    # 持久化 & 恢复
    db2 = DB(DB_PATH)
    queue2 = NotifyQueue(db2)
    log("T2", queue2.size >= 2, f"恢复待发送: {queue2.size}")

    # 重试逻辑
    test_item = NotifyItem("test", "测试", max_retries=3)
    test_item.retry_count = 2
    log("T2", test_item.should_retry, "重试次数未达上限")
    test_item.retry_count = 3
    log("T2", not test_item.should_retry, "重试次数已达上限")

    # 指数退避
    test_item2 = NotifyItem("test", "测试", max_retries=3)
    test_item2.retry_count = 0
    delays = []
    for _ in range(3):
        delays.append(test_item2.next_retry_delay)
        test_item2.retry_count += 1
    log("T2", delays[0] < delays[1] < delays[2], f"退避递增: {delays}")

    # ── T3: 申诉分配 ─────────────────
    print("\n[T3] 申诉分配")
    print("─" * 60)

    # 设置 TA 列表
    os.environ["TA_LIST"] = "ta_001,ta_002,ta_003"

    # 创建申诉
    appeal1 = Appeal(
        id="appeal_001",
        submission_id=sub1.id,
        student_id="student_001",
        reason="分数太低",
        created_at=datetime.now(),
        status="pending",
    )
    db.save_appeal(appeal1)

    # 自动分配
    assigned = ta._auto_assign_appeal(appeal1)
    log("T3", assigned is not None, f"自动分配: {assigned}")

    # 手动分配
    ta.assign_appeal("appeal_001", "ta_002")
    log("T3", ta.get_assigned_ta("appeal_001") == "ta_002", "手动分配")

    # 负载均衡: 连续分配应该轮转
    ta._assignments.clear()
    appeal2 = Appeal(
        id="appeal_002",
        submission_id=sub2.id,
        student_id="student_002",
        reason="不同意",
        created_at=datetime.now(),
        status="pending",
    )
    db.save_appeal(appeal2)

    assigned1 = ta._auto_assign_appeal(appeal1)
    ta._assignments[appeal1.id] = assigned1
    assigned2 = ta._auto_assign_appeal(appeal2)
    log("T3", assigned1 is not None and assigned2 is not None, "负载均衡分配")

    # 清理环境变量
    del os.environ["TA_LIST"]

    # ── T4: 审批历史记录 ─────────────────
    print("\n[T4] 审批历史记录")
    print("─" * 60)

    # 记录多种操作
    ta._log_ta_action("appeal_001", "ta_001", "approved", "同意申诉", 85.0)
    ta._log_ta_action("appeal_002", "ta_001", "rejected", "理由不充分")
    ta._log_ta_action("appeal_001", "ta_002", "assigned", "分配给TA")
    ta._log_ta_action("appeal_001", "system", "pushed", "推送到频道")

    # 按 TA 查询
    history = ta._get_ta_actions(ta_id="ta_001")
    log("T4", len(history) == 2, f"TA001 操作数: {len(history)}")

    # 按申诉查询
    history2 = ta._get_ta_actions(appeal_id="appeal_001")
    log("T4", len(history2) >= 3, f"申诉001 操作数: {len(history2)} (含分配和推送)")

    # 全部查询
    all_actions = ta._get_ta_actions(limit=100)
    log("T4", len(all_actions) >= 4, f"总操作数: {len(all_actions)}")

    # 操作内容验证
    approved = [a for a in all_actions if a["action"] == "approved"]
    log("T4", len(approved) >= 1, "审批记录存在")
    if approved:
        log("T4", approved[0].get("new_score") == 85.0, f"新分数: {approved[0].get('new_score')}")

    # ── T5: TA 工作台指令处理 ─────────────────
    print("\n[T5] TA 工作台指令处理")
    print("─" * 60)

    # 创建 AppealHandler
    appeal_handler = AppealHandler(db, engine)

    # 创建新申诉用于测试
    sub3 = homework.receive("asgn_test", "student_003", "王五", "我的作业内容不太好。")
    engine.grade(sub3, rubric, "测试作业")
    sub3 = db.get_submission(sub3.id)  # 刷新
    appeal3 = appeal_handler.submit_appeal(sub3.id, "student_003", "我觉得可以更好")
    appeal3 = db.get_appeal(appeal3.id)  # 刷新

    # /待审列表
    mock_msg = QQMessage(
        user_id="ta_001",
        group_id=None,
        message_id="msg_001",
        content="/待审列表",
        timestamp=datetime.now(),
    )
    result = await ta.handle_ta_command(mock_msg, TACommand.PENDING, {})
    log("T5", "待处理" in result or "没有" in result, f"/待审列表: {result[:30]}")

    # /处理
    result = await ta.handle_ta_command(
        mock_msg, TACommand.HANDLE, {"appeal_id": appeal3.id}
    )
    log("T5", "已接手" in result, f"/处理: {result[:30]}")

    # /历史
    result = await ta.handle_ta_command(mock_msg, TACommand.HISTORY, {})
    log("T5", len(result) > 0, f"/历史: {result[:30]}")

    # /统计
    result = await ta.handle_ta_command(mock_msg, TACommand.STATS, {})
    log("T5", "统计" in result, f"/统计: {result[:30]}")

    # /帮助
    result = await ta.handle_ta_command(mock_msg, TACommand.HELP, {})
    log("T5", "指令" in result, f"/帮助: {result[:30]}")

    # /同意 — 对一个新申诉
    sub4 = homework.receive("asgn_test", "student_004", "赵六", "另一个作业。")
    engine.grade(sub4, rubric, "测试作业")
    sub4 = db.get_submission(sub4.id)
    appeal4 = appeal_handler.submit_appeal(sub4.id, "student_004", "重新评一下")
    appeal4 = db.get_appeal(appeal4.id)

    result = await ta.handle_ta_command(
        mock_msg, TACommand.APPROVE,
        {"appeal_id": appeal4.id, "note": "同意，确实偏低"},
        appeal_handler=appeal_handler,  # 传入 appeal_handler
    )
    log("T5", "已批准" in result, f"/同意: {result[:30]}")

    # 验证申诉状态已更新
    appeal4_check = db.get_appeal(appeal4.id)
    log("T5", appeal4_check.status == "approved", f"申诉状态: {appeal4_check.status}")

    # /驳回 — 对另一个新申诉
    sub5 = homework.receive("asgn_test", "student_005", "钱七", "又一个作业。")
    engine.grade(sub5, rubric, "测试作业")
    sub5 = db.get_submission(sub5.id)
    appeal5 = appeal_handler.submit_appeal(sub5.id, "student_005", "我不同意")
    appeal5 = db.get_appeal(appeal5.id)

    result = await ta.handle_ta_command(
        mock_msg, TACommand.REJECT,
        {"appeal_id": appeal5.id, "note": "理由不成立"},
        appeal_handler=appeal_handler,  # 传入 appeal_handler
    )
    log("T5", "已驳回" in result, f"/驳回: {result[:30]}")

    appeal5_check = db.get_appeal(appeal5.id)
    log("T5", appeal5_check.status == "rejected", f"申诉状态: {appeal5_check.status}")

    # ── T6: 申诉推送（含 QQ 频道） ─────────────────
    print("\n[T6] 申诉推送")
    print("─" * 60)

    # Mock 模式（无 adapter）
    push_result = await ta.push_appeal_to_ta_channel(appeal1, sub1)
    log("T6", isinstance(push_result, bool), f"推送返回: {push_result}")

    # 格式化消息包含新指令提示
    push_msg = ta.format_appeal_push(appeal1, sub1)
    log("T6", "工作台指令" in push_msg, "推送消息包含工作台指令")
    log("T6", appeal1.id in push_msg, f"推送消息包含申诉ID: {appeal1.id}")

    # ── T7: DB 新表验证 ─────────────────
    print("\n[T7] DB 新表验证")
    print("─" * 60)

    # notify_queue 表
    rows = db._conn.execute("SELECT COUNT(*) as cnt FROM notify_queue").fetchone()
    log("T7", rows["cnt"] >= 2, f"notify_queue 行数: {rows['cnt']}")

    # ta_actions 表
    rows2 = db._conn.execute("SELECT COUNT(*) as cnt FROM ta_actions").fetchone()
    log("T7", rows2["cnt"] >= 5, f"ta_actions 行数: {rows2['cnt']}")

    # ── T8: 通知优先级验证 ─────────────────
    print("\n[T8] 通知优先级")
    print("─" * 60)

    log("T8", NotifyPriority.LOW < NotifyPriority.NORMAL, "LOW < NORMAL")
    log("T8", NotifyPriority.NORMAL < NotifyPriority.HIGH, "NORMAL < HIGH")
    log("T8", NotifyPriority.HIGH < NotifyPriority.URGENT, "HIGH < URGENT")

    # 优先级排序测试 — 用新的 DB 避免旧数据干扰
    db3_path = os.path.join(tempfile.mkdtemp(), "test_priority.db")
    db3 = DB(db3_path)
    queue3 = NotifyQueue(db3)
    queue3.enqueue("u1", "低", NotifyPriority.LOW)
    queue3.enqueue("u2", "紧急", NotifyPriority.URGENT)
    queue3.enqueue("u3", "普通", NotifyPriority.NORMAL)
    queue3.enqueue("u4", "高", NotifyPriority.HIGH)

    priorities = []
    while queue3.size > 0:
        item = queue3.dequeue()
        priorities.append(item.priority)

    expected_order = [NotifyPriority.URGENT, NotifyPriority.HIGH, NotifyPriority.NORMAL, NotifyPriority.LOW]
    log("T8", priorities == expected_order, f"优先级排序: {priorities}")

    # ── 最终结果 ─────────────────
    print("\n" + "=" * 60)
    print(f" 测试结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 60)

    # 写日志
    with open("test_m09_report.log", "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))

    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
