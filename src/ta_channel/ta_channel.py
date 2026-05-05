"""
TA 渠道管理（完整版）
处理申诉推送、TA 工作台指令、通知队列、审批历史

功能:
1. 申诉产生时自动推送到助教QQ频道/群
2. TA 工作台指令系统（查看待审、指定处理、审批）
3. 异步通知队列（重试、优先级、持久化）
4. 申诉分配（避免重复处理）
5. 审批历史记录（可追溯）
"""
from __future__ import annotations
import re
import uuid
import time
import logging
import threading
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Callable, Awaitable
from enum import Enum

from contracts.models import Appeal, HomeworkSubmission, QQMessage
from storage.db import DB

logger = logging.getLogger("ta_channel")


# ─────────────────────────────────────────────
# 通知优先级
# ─────────────────────────────────────────────

class NotifyPriority:
    """通知优先级"""
    LOW = 0       # 一般通知
    NORMAL = 1    # 批改结果
    HIGH = 2     # 申诉推送
    URGENT = 3   # 申诉审批结果


# ─────────────────────────────────────────────
# 通知队列项
# ─────────────────────────────────────────────

class NotifyItem:
    """通知队列中的待发送项"""

    def __init__(
        self,
        target_id: str,
        content: str,
        priority: int = NotifyPriority.NORMAL,
        group_id: Optional[str] = None,
        max_retries: int = 3,
        item_id: Optional[str] = None,
    ):
        self.id = item_id or f"ntf_{uuid.uuid4().hex[:8]}"
        self.target_id = target_id      # 接收者 ID
        self.group_id = group_id        # 群/频道 ID（可选）
        self.content = content
        self.priority = priority
        self.max_retries = max_retries
        self.retry_count = 0
        self.status = "pending"         # pending / sent / failed
        self.created_at = datetime.now()
        self.last_attempt = None
        self.error_msg = None

    @property
    def should_retry(self) -> bool:
        return self.retry_count < self.max_retries and self.status != "sent"

    @property
    def next_retry_delay(self) -> float:
        """指数退避: 5s, 15s, 45s"""
        return min(5 * (3 ** self.retry_count), 120)


# ─────────────────────────────────────────────
# 通知队列（持久化 + 异步）
# ─────────────────────────────────────────────

class NotifyQueue:
    """
    异步通知队列
    - 按优先级排序发送
    - 失败自动重试（指数退避）
    - 持久化到 SQLite（重启可恢复）
    - 后台线程消费
    """

    def __init__(self, db: DB, send_func: Optional[Callable] = None):
        """
        Args:
            db: 数据库（用于持久化通知记录）
            send_func: 异步发送函数
                签名: async def send(target_id, content, group_id=None) -> bool
        """
        self.db = db
        self._send_func = send_func
        self._queue: List[NotifyItem] = []
        self._lock = threading.Lock()
        self._running = False
        self._consumer_thread: Optional[threading.Thread] = None
        self._loop = None  # asyncio event loop for sending

        # 启动时恢复未发送的通知
        self._recover_pending()

    def _recover_pending(self):
        """从 SQLite 恢复未发送的通知"""
        try:
            rows = self.db._conn.execute(
                "SELECT * FROM notify_queue WHERE status='pending' "
                "ORDER BY priority DESC, created_at ASC"
            ).fetchall()
            for row in rows:
                item = NotifyItem(
                    target_id=row["target_id"],
                    content=row["content"],
                    priority=row["priority"],
                    group_id=row["group_id"],
                    max_retries=row["max_retries"],
                    item_id=row["id"],
                )
                item.retry_count = row["retry_count"]
                item.status = row["status"]
                item.created_at = datetime.fromisoformat(row["created_at"])
                self._queue.append(item)
            if self._queue:
                logger.info(f"恢复了 {len(self._queue)} 条待发送通知")
        except Exception as e:
            logger.warning(f"恢复通知失败: {e}")

    def enqueue(
        self,
        target_id: str,
        content: str,
        priority: int = NotifyPriority.NORMAL,
        group_id: Optional[str] = None,
        max_retries: int = 3,
    ) -> NotifyItem:
        """
        入队通知（线程安全）

        Args:
            target_id: 接收者 ID
            content: 消息内容
            priority: 优先级
            group_id: 群/频道 ID
            max_retries: 最大重试次数
        """
        item = NotifyItem(
            target_id=target_id,
            content=content,
            priority=priority,
            group_id=group_id,
            max_retries=max_retries,
        )

        with self._lock:
            # 按优先级插入（高优先级在前）
            inserted = False
            for i, existing in enumerate(self._queue):
                if item.priority > existing.priority:
                    self._queue.insert(i, item)
                    inserted = True
                    break
            if not inserted:
                self._queue.append(item)

        # 持久化
        self._persist_item(item)

        logger.info(
            f"通知入队: {item.id} → {target_id}, "
            f"优先级={priority}, 队列长度={len(self._queue)}"
        )
        return item

    def _persist_item(self, item: NotifyItem):
        """持久化通知项到 SQLite"""
        try:
            self.db._execute_write(
                "INSERT OR REPLACE INTO notify_queue "
                "(id, target_id, group_id, content, priority, "
                "status, retry_count, max_retries, created_at, error_msg) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    item.id, item.target_id, item.group_id,
                    item.content, item.priority,
                    item.status, item.retry_count, item.max_retries,
                    item.created_at.isoformat(), item.error_msg,
                ),
            )
        except Exception as e:
            logger.error(f"持久化通知失败: {e}")

    def _update_item(self, item: NotifyItem):
        """更新通知项状态"""
        self._persist_item(item)

    def dequeue(self) -> Optional[NotifyItem]:
        """出队一条通知（按优先级）"""
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def peek(self) -> Optional[NotifyItem]:
        """查看队首（不出队）"""
        with self._lock:
            if not self._queue:
                return None
            return self._queue[0]

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len([i for i in self._queue if i.status == "pending"])

    def start_consumer(self, loop=None):
        """启动后台消费线程"""
        self._loop = loop
        self._running = True
        self._consumer_thread = threading.Thread(
            target=self._consume_loop, daemon=True
        )
        self._consumer_thread.start()
        logger.info("通知队列消费者已启动")

    def stop_consumer(self):
        """停止消费"""
        self._running = False
        if self._consumer_thread:
            self._consumer_thread.join(timeout=5)
        logger.info("通知队列消费者已停止")

    def _consume_loop(self):
        """消费循环（后台线程）"""
        import asyncio

        while self._running:
            item = self.dequeue()
            if item is None:
                time.sleep(0.5)
                continue

            if self._send_func and self._loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._send_func(
                            item.target_id, item.content, item.group_id
                        ),
                        self._loop,
                    )
                    result = future.result(timeout=30)
                    if result:
                        item.status = "sent"
                        logger.info(f"通知发送成功: {item.id}")
                    else:
                        self._handle_send_failure(item, "send returned False")
                except Exception as e:
                    self._handle_send_failure(item, str(e))
            else:
                # 无发送函数 — 标记为已发送（mock 模式）
                item.status = "sent"
                logger.debug(f"[Mock] 通知已标记发送: {item.id}")

            self._update_item(item)

    def _handle_send_failure(self, item: NotifyItem, error: str):
        """处理发送失败"""
        item.retry_count += 1
        item.last_attempt = datetime.now()
        item.error_msg = error

        if item.should_retry:
            # 重新入队
            with self._lock:
                self._queue.append(item)
            logger.warning(
                f"通知发送失败 (重试 {item.retry_count}/{item.max_retries}): "
                f"{item.id}, 错误: {error}"
            )
        else:
            item.status = "failed"
            logger.error(
                f"通知发送彻底失败: {item.id}, 错误: {error}"
            )


# ─────────────────────────────────────────────
# TA 指令解析
# ─────────────────────────────────────────────

class TACommand:
    """TA 工作台指令"""

    # 指令列表
    PENDING = "pending"         # /待审列表
    HANDLE = "handle"           # /处理 申诉ID
    APPROVE = "approve"         # /同意 申诉ID 备注
    REJECT = "reject"           # /驳回 申诉ID 备注
    HISTORY = "history"         # /历史
    STATS = "stats"             # /统计
    HELP = "help"               # /帮助


class TACommandParser:
    """解析 TA 的指令消息"""

    # 指令正则
    PATTERNS = [
        # /待审列表 /pending
        (re.compile(r'^[/／]?(待审列表|待审|pending|待处理)$', re.I),
         TACommand.PENDING, None),
        # /处理 申诉ID
        (re.compile(r'^[/／]?(处理|handle)\s+(\S+)', re.I),
         TACommand.HANDLE, "appeal_id"),
        # /同意 申诉ID [备注]
        (re.compile(r'^[/／]?(同意|批准|approve)\s+(\S+)(?:\s+(.+))?$', re.I),
         TACommand.APPROVE, "appeal_id_note"),
        # /驳回 申诉ID [备注]
        (re.compile(r'^[/／]?(驳回|拒绝|reject)\s+(\S+)(?:\s+(.+))?$', re.I),
         TACommand.REJECT, "appeal_id_note"),
        # /历史 /history
        (re.compile(r'^[/／]?(历史|history|审批记录)$', re.I),
         TACommand.HISTORY, None),
        # /统计 /stats
        (re.compile(r'^[/／]?(统计|stats)$', re.I),
         TACommand.STATS, None),
        # /帮助 /help
        (re.compile(r'^[/／]?(帮助|help|指令)$', re.I),
         TACommand.HELP, None),
    ]

    @classmethod
    def parse(cls, message: str) -> Tuple[str, Dict]:
        """
        解析 TA 指令

        Returns:
            (command, params) — command 为 TACommand 中的值
            如果不匹配任何指令，返回 ("unknown", {})
        """
        text = message.strip()
        for pattern, cmd, param_name in cls.PATTERNS:
            m = pattern.match(text)
            if m:
                params = {}
                if param_name == "appeal_id":
                    params["appeal_id"] = m.group(2)
                elif param_name == "appeal_id_note":
                    params["appeal_id"] = m.group(2)
                    if m.lastindex and m.lastindex >= 3:
                        params["note"] = m.group(3) or ""
                    else:
                        params["note"] = ""
                return cmd, params

        return "unknown", {}

    @classmethod
    def is_ta_command(cls, message: str) -> bool:
        """判断消息是否为 TA 指令"""
        cmd, _ = cls.parse(message)
        return cmd != "unknown"

    @classmethod
    def get_help_text(cls) -> str:
        """返回帮助文本"""
        return (
            "📋 TA 工作台指令\n"
            "━━━━━━━━━━━━━━\n"
            "/待审列表 — 查看所有待审申诉\n"
            "/处理 申诉ID — 接手处理指定申诉\n"
            "/同意 申诉ID [备注] — 批准申诉\n"
            "/驳回 申诉ID [备注] — 驳回申诉\n"
            "/历史 — 查看最近审批记录\n"
            "/统计 — 查看审批统计\n"
            "/帮助 — 显示此帮助\n"
            "\n"
            "快捷回复:\n"
            "  「同意申诉」或「1」— 批准当前申诉\n"
            "  「驳回申诉」或「2」— 驳回当前申诉"
        )


# ─────────────────────────────────────────────
# TA 频道管理器（完整版）
# ─────────────────────────────────────────────

class TAChannelManager:
    """
    TA 渠道管理（完整版）
    - 申诉推送（自动分配 + 频道通知）
    - TA 工作台指令系统
    - 异步通知队列
    - 审批历史记录
    """

    def __init__(
        self,
        db: DB,
        qclaw_adapter=None,
        ta_group_id: Optional[str] = None,
    ):
        """
        Args:
            db: 数据据库
            qclaw_adapter: QQ Bot 适配器（用于真实推送）
            ta_group_id: TA 专属群/频道 ID（用于推送申诉）
        """
        self.db = db
        self.adapter = qclaw_adapter
        self.ta_group_id = ta_group_id or os.environ.get(
            "TA_GROUP_ID", ""
        )

        # 通知队列
        self._notify_queue: Optional[NotifyQueue] = None
        if self.adapter:
            self._notify_queue = NotifyQueue(
                db=self.db,
                send_func=self._do_send_notification,
            )

        # 申诉分配: appeal_id → ta_user_id
        self._assignments: Dict[str, str] = {}

    async def _do_send_notification(
        self,
        target_id: str,
        content: str,
        group_id: Optional[str] = None,
    ) -> bool:
        """实际发送通知（通过 QclawAdapter）"""
        try:
            if group_id:
                await self.adapter.send_group_message(group_id, content)
            else:
                await self.adapter.send_message(target_id, content)
            return True
        except Exception as e:
            logger.error(f"发送通知失败: target={target_id}, error={e}")
            return False

    def get_notify_queue(self) -> Optional[NotifyQueue]:
        """获取通知队列实例"""
        return self._notify_queue

    # ── 申诉推送 ────────────────────────────

    async def push_appeal_to_ta_channel(
        self,
        appeal: Appeal,
        submission: Optional[HomeworkSubmission] = None,
    ) -> bool:
        """
        将申诉推送到 TA 频道/群
        1. 格式化申诉信息
        2. 自动分配给空闲 TA
        3. 发送到 TA 群 + 分配的 TA 私聊

        Returns:
            是否推送成功
        """
        # 自动分配
        assigned_ta = self._auto_assign_appeal(appeal)
        if assigned_ta:
            self._assignments[appeal.id] = assigned_ta

        # 格式化推送消息
        push_msg = self.format_appeal_push(appeal, submission)

        if assigned_ta:
            push_msg += f"\n👤 已分配给: {assigned_ta}"

        # 记录推送日志
        self._log_ta_action(
            appeal_id=appeal.id,
            ta_id="system",
            action="pushed",
            note=f"推送申诉到 TA 频道, 分配给 {assigned_ta or '无人'}",
        )

        success = False

        # 1. 推送到 TA 群
        if self.ta_group_id and self.adapter:
            try:
                await self.adapter.send_group_message(
                    self.ta_group_id, push_msg
                )
                success = True
                logger.info(f"申诉推送成功到 TA 群: {appeal.id}")
            except Exception as e:
                logger.error(f"推送 TA 群失败: {e}")

        # 2. 通知分配的 TA（私聊）
        if assigned_ta and self.adapter:
            try:
                await self.adapter.send_message(assigned_ta, push_msg)
                success = True
                logger.info(f"申诉推送成功到 TA {assigned_ta}: {appeal.id}")
            except Exception as e:
                logger.error(f"推送 TA 私聊失败: {e}")

        # 3. 使用通知队列（异步，有重试）
        if self._notify_queue:
            if self.ta_group_id:
                self._notify_queue.enqueue(
                    target_id="",
                    content=push_msg,
                    priority=NotifyPriority.HIGH,
                    group_id=self.ta_group_id,
                )
            if assigned_ta:
                self._notify_queue.enqueue(
                    target_id=assigned_ta,
                    content=push_msg,
                    priority=NotifyPriority.HIGH,
                )
            success = True

        if not success:
            logger.warning(
                f"申诉推送未发送（无 adapter 和通知队列）: {appeal.id}"
            )

        return success

    def format_appeal_push(self, appeal: Appeal,
                           submission: Optional[HomeworkSubmission] = None) -> str:
        """
        格式化申诉信息，推送给 TA
        """
        lines = [
            "🔔 新的申诉请求",
            "━━━━━━━━━━━━━━",
            f"📋 申诉ID: {appeal.id}",
            f"👤 学生ID: {appeal.student_id}",
            f"📝 提交ID: {appeal.submission_id}",
            f"💬 申诉原因: {appeal.reason}",
        ]

        if submission:
            lines.extend([
                f"📊 当前分数: {submission.score or '未评分'}",
                f"💬 当前反馈: {(submission.feedback or '无')[:100]}",
            ])

        lines.extend([
            "",
            "📝 快捷回复:",
            "  「同意申诉」或「1」— 批准申诉并重新评分",
            "  「驳回申诉」或「2」— 维持原评分",
            "",
            "📋 工作台指令:",
            "  /待审列表 — 查看所有待审申诉",
            "  /同意 {0} [备注] — 批准指定申诉".format(appeal.id),
            "  /驳回 {0} [备注] — 驳回指定申诉".format(appeal.id),
        ])

        return "\n".join(lines)

    # ── 申诉分配 ────────────────────────────

    def _auto_assign_appeal(self, appeal: Appeal) -> Optional[str]:
        """
        自动分配申诉给空闲 TA
        策略: 找当前处理申诉最少的 TA

        Returns:
            分配的 TA user_id，无可用 TA 时返回 None
        """
        # 从 DB 获取所有 TA 列表
        ta_list = self._get_ta_list()
        if not ta_list:
            return None

        # 统计每个 TA 当前处理的申诉数
        ta_loads: Dict[str, int] = {ta: 0 for ta in ta_list}
        for aid, tid in self._assignments.items():
            if tid in ta_loads:
                ta_loads[tid] += 1

        # 选择负载最低的
        assigned = min(ta_list, key=lambda t: ta_loads[t])
        return assigned

    def _get_ta_list(self) -> List[str]:
        """获取 TA 列表"""
        # 优先从环境变量读取
        ta_list_str = os.environ.get("TA_LIST", "")
        if ta_list_str:
            return [t.strip() for t in ta_list_str.split(",") if t.strip()]

        # 从 DB 中读取有 TA 角色的用户
        try:
            rows = self.db._conn.execute(
                "SELECT DISTINCT user_id FROM ta_actions "
                "ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            return [r["user_id"] for r in rows]
        except Exception:
            return []

    def get_assigned_ta(self, appeal_id: str) -> Optional[str]:
        """获取申诉分配的 TA"""
        return self._assignments.get(appeal_id)

    def assign_appeal(self, appeal_id: str, ta_id: str):
        """手动分配申诉给指定 TA"""
        self._assignments[appeal_id] = ta_id
        self._log_ta_action(
            appeal_id=appeal_id,
            ta_id=ta_id,
            action="assigned",
            note=f"手动分配给 {ta_id}",
        )

    # ── TA 指令处理 ──────────────────────────

    async def handle_ta_command(
        self,
        message: QQMessage,
        command: str,
        params: Dict,
        appeal_handler=None,
    ) -> Optional[str]:
        """
        处理 TA 工作台指令

        Args:
            message: 原始 QQ 消息
            command: 解析后的指令
            params: 指令参数
            appeal_handler: AppealHandler 实例

        Returns:
            回复文本
        """
        if command == TACommand.PENDING:
            return self._cmd_pending()
        elif command == TACommand.HANDLE:
            return self._cmd_handle(message.user_id, params)
        elif command == TACommand.APPROVE:
            return await self._cmd_approve(
                message.user_id, params, appeal_handler
            )
        elif command == TACommand.REJECT:
            return await self._cmd_reject(
                message.user_id, params, appeal_handler
            )
        elif command == TACommand.HISTORY:
            return self._cmd_history(message.user_id)
        elif command == TACommand.STATS:
            return self._cmd_stats()
        elif command == TACommand.HELP:
            return TACommandParser.get_help_text()
        else:
            return "⚠️ 未知指令。输入 /帮助 查看可用指令"

    def _cmd_pending(self) -> str:
        """处理 /待审列表 指令"""
        appeals = self.db.list_pending_appeals()
        if not appeals:
            return "📭 当前没有待处理的申诉"

        lines = [f"📬 待处理申诉: {len(appeals)} 件\n"]
        for a in appeals:
            assigned = self._assignments.get(a.id, "")
            assign_tag = f" → @{assigned}" if assigned else ""
            lines.append(
                f"  📋 {a.id} | 学生 {a.student_id} | "
                f"{a.reason[:30]}{assign_tag}"
            )
        lines.append("\n输入 /帮助 查看指令列表")
        return "\n".join(lines)

    def _cmd_handle(self, ta_id: str, params: Dict) -> str:
        """处理 /处理 申诉ID 指令"""
        appeal_id = params.get("appeal_id", "")
        appeal = self.db.get_appeal(appeal_id)
        if not appeal:
            return f"⚠️ 申诉不存在: {appeal_id}"
        if appeal.status != "pending":
            return f"⚠️ 申诉已处理: {appeal_id} (状态: {appeal.status})"

        self.assign_appeal(appeal_id, ta_id)
        return (
            f"✅ 已接手申诉 {appeal_id}\n"
            f"学生: {appeal.student_id}\n"
            f"原因: {appeal.reason}\n"
            f"回复 /同意 {appeal_id} 或 /驳回 {appeal_id} 进行审批"
        )

    async def _cmd_approve(
        self, ta_id: str, params: Dict, appeal_handler=None
    ) -> str:
        """处理 /同意 申诉ID [备注] 指令"""
        appeal_id = params.get("appeal_id", "")
        note = params.get("note", "")

        if not appeal_handler:
            return "⚠️ 申诉处理器未初始化"

        appeal = self.db.get_appeal(appeal_id)
        if not appeal:
            return f"⚠️ 申诉不存在: {appeal_id}"
        if appeal.status != "pending":
            return f"⚠️ 申诉已处理: {appeal_id} (状态: {appeal.status})"

        try:
            result = appeal_handler.process_appeal(
                appeal_id, approved=True, reviewer_id=ta_id
            )
            # 记录审批历史
            self._log_ta_action(
                appeal_id=appeal_id,
                ta_id=ta_id,
                action="approved",
                note=note,
                new_score=result.new_score,
            )
            msg = f"✅ 申诉 {appeal_id} 已批准"
            if result.new_score is not None:
                msg += f"，新分数: {result.new_score}"
            if note:
                msg += f"\n📝 备注: {note}"
            return msg
        except Exception as e:
            return f"❌ 审批失败: {e}"

    async def _cmd_reject(
        self, ta_id: str, params: Dict, appeal_handler=None
    ) -> str:
        """处理 /驳回 申诉ID [备注] 指令"""
        appeal_id = params.get("appeal_id", "")
        note = params.get("note", "")

        if not appeal_handler:
            return "⚠️ 申诉处理器未初始化"

        appeal = self.db.get_appeal(appeal_id)
        if not appeal:
            return f"⚠️ 申诉不存在: {appeal_id}"
        if appeal.status != "pending":
            return f"⚠️ 申诉已处理: {appeal_id} (状态: {appeal.status})"

        try:
            result = appeal_handler.process_appeal(
                appeal_id, approved=False, reviewer_id=ta_id
            )
            # 记录审批历史
            self._log_ta_action(
                appeal_id=appeal_id,
                ta_id=ta_id,
                action="rejected",
                note=note,
            )
            msg = f"❌ 申诉 {appeal_id} 已驳回，维持原评分"
            if note:
                msg += f"\n📝 备注: {note}"
            return msg
        except Exception as e:
            return f"❌ 审批失败: {e}"

    def _cmd_history(self, ta_id: str) -> str:
        """处理 /历史 指令"""
        actions = self._get_ta_actions(ta_id=ta_id, limit=10)
        if not actions:
            return "📭 暂无审批记录"

        lines = [f"📜 最近审批记录 ({ta_id}):\n"]
        for a in actions:
            icon = {"approved": "✅", "rejected": "❌", "assigned": "👤",
                    "pushed": "🔔"}.get(a["action"], "📝")
            note_text = f" — {a['note']}" if a.get("note") else ""
            score_text = f" (新分: {a['new_score']})" if a.get("new_score") else ""
            lines.append(
                f"  {icon} [{a['created_at'][:16]}] "
                f"申诉 {a['appeal_id']}: {a['action']}"
                f"{note_text}{score_text}"
            )
        return "\n".join(lines)

    def _cmd_stats(self) -> str:
        """处理 /统计 指令"""
        actions = self._get_ta_actions(limit=1000)
        if not actions:
            return "📭 暂无审批数据"

        # 统计
        total = len(actions)
        approved = len([a for a in actions if a["action"] == "approved"])
        rejected = len([a for a in actions if a["action"] == "rejected"])
        pending_count = len(self.db.list_pending_appeals())

        # 按 TA 统计
        ta_stats: Dict[str, int] = {}
        for a in actions:
            if a["action"] in ("approved", "rejected"):
                ta = a["ta_id"]
                ta_stats[ta] = ta_stats.get(ta, 0) + 1

        lines = [
            "📊 审批统计",
            "━━━━━━━━━━━━━━",
            f"📋 总审批数: {total}",
            f"✅ 已批准: {approved}",
            f"❌ 已驳回: {rejected}",
            f"🔔 待处理: {pending_count}",
        ]

        if ta_stats:
            lines.append("\n👤 TA 工作量:")
            for ta, count in sorted(
                ta_stats.items(), key=lambda x: -x[1]
            ):
                lines.append(f"  {ta}: {count} 件")

        return "\n".join(lines)

    # ── 兼容旧接口 ──────────────────────────

    def parse_ta_instruction(self, ta_message: str) -> Tuple[str, Optional[str]]:
        """
        解析 TA 指令（兼容旧版接口）

        Returns:
            (action, note) — action 为 "approve" / "reject" / "unknown"
            note 为 TA 附带的备注（如有）
        """
        text = ta_message.strip()

        # 先尝试工作台指令
        cmd, params = TACommandParser.parse(text)
        if cmd == TACommand.APPROVE:
            return "approve", params.get("note", "")
        elif cmd == TACommand.REJECT:
            return "reject", params.get("note", "")

        # 兼容旧的快捷回复格式
        if re.search(r"同意|批准|通过|确认", text) or text == "1":
            note = re.sub(
                r"^(同意|批准|通过|确认|1)[\s：:，,]*",
                "", text,
            ).strip()
            return "approve", note if note else None

        if re.search(r"驳回|拒绝|不同意|否决", text) or text == "2":
            note = re.sub(
                r"^(驳回|拒绝|不同意|否决|2)[\s：:，,]*",
                "", text,
            ).strip()
            return "reject", note if note else None

        return "unknown", None

    def confirm_decision(self, appeal_id: str,
                         action: str, note: Optional[str] = None) -> str:
        """生成确认信息（兼容旧接口）"""
        if action == "approve":
            msg = f"✅ 申诉 {appeal_id} 已批准，将重新评分"
        elif action == "reject":
            msg = f"❌ 申诉 {appeal_id} 已驳回，维持原评分"
        else:
            msg = f"⚠️ 无法识别的指令，请回复「同意」或「驳回」"

        if note:
            msg += f"\n📝 TA备注: {note}"

        return msg

    def get_pending_appeals_summary(self) -> str:
        """获取待处理申诉摘要（兼容旧接口）"""
        return self._cmd_pending()

    # ── 审批历史记录 ────────────────────────

    def _log_ta_action(
        self,
        appeal_id: str,
        ta_id: str,
        action: str,
        note: Optional[str] = None,
        new_score: Optional[float] = None,
    ):
        """记录 TA 操作日志"""
        try:
            action_id = f"ta_act_{uuid.uuid4().hex[:8]}"
            self.db._execute_write(
                "INSERT INTO ta_actions "
                "(id, appeal_id, ta_id, action, note, new_score, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    action_id, appeal_id, ta_id, action,
                    note, new_score, datetime.now().isoformat(),
                ),
            )
        except Exception as e:
            logger.error(f"记录 TA 操作失败: {e}")

    def _get_ta_actions(
        self,
        ta_id: Optional[str] = None,
        appeal_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """查询 TA 操作历史"""
        try:
            sql = "SELECT * FROM ta_actions WHERE 1=1"
            params: list = []
            if ta_id:
                sql += " AND ta_id=?"
                params.append(ta_id)
            if appeal_id:
                sql += " AND appeal_id=?"
                params.append(appeal_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = self.db._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"查询 TA 操作失败: {e}")
            return []

    # ── 通知辅助 ────────────────────────────

    def send_notification(
        self,
        target_id: str,
        content: str,
        priority: int = NotifyPriority.NORMAL,
        group_id: Optional[str] = None,
    ):
        """
        发送通知（通过通知队列或直接）
        """
        if self._notify_queue:
            self._notify_queue.enqueue(
                target_id=target_id,
                content=content,
                priority=priority,
                group_id=group_id,
            )
        elif self.adapter:
            # 无队列时的同步 fallback
            logger.warning(
                "通知队列未初始化，使用同步发送（不推荐）"
            )
        else:
            logger.info(
                f"[Mock] 通知: → {target_id or group_id}: "
                f"{content[:50]}..."
            )


# 模块级导入（os 在 NotifyQueue 使用）
import os
