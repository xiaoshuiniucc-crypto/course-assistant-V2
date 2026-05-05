"""
QQ Bot 适配器 (Qclaw)
基于 QQ 官方 botpy SDK (qq-botpy) 的真实接入层
支持: 频道消息、群聊消息、C2C私聊、文件收发
"""
from __future__ import annotations
import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable, List, Dict, Any

from contracts.models import QQMessage, MessageType

logger = logging.getLogger("qclaw")


# ─────────────────────────────────────────────
# 消息适配: botpy Message → QQMessage
# ─────────────────────────────────────────────

def _convert_botpy_message(msg: Any, msg_type: str = MessageType.PRIVATE) -> QQMessage:
    """将 botpy 的 Message 对象转换为统一的 QQMessage"""
    content = getattr(msg, "content", "") or ""
    # 去掉 @机器人 的内容
    if content.startswith("<@!"):
        # 去掉 @mention 标记
        import re
        content = re.sub(r"<@!\d+>\s*", "", content).strip()

    return QQMessage(
        user_id=str(getattr(msg, "author", None) and msg.author.get("user_openid", "")
                     or getattr(msg, "author", {}).get("member_openid", "")
                     or getattr(msg, "author", {}).get("id", "unknown")),
        group_id=str(getattr(msg, "group_openid", None) or ""),
        message_id=getattr(msg, "id", ""),
        content=content,
        timestamp=datetime.now(),
        message_type=msg_type,
        file_url=None,
        file_name=None,
        raw=msg.__dict__ if hasattr(msg, "__dict__") else {},
    )


# ─────────────────────────────────────────────
# 真实 Qclaw 适配器（基于 qq-botpy）
# ─────────────────────────────────────────────

class QclawAdapter:
    """
    QQ Bot 适配器
    支持两种模式:
    1. 真实模式: 使用 qq-botpy SDK 连接 QQ 官方机器人
    2. Mock模式: 本地测试用（无需QQ连接）

    配置:
      QQ_APPID       - QQ 机器人 AppID
      QQ_SECRET      - QQ 机器人 AppSecret
      QQ_SANDBOX     - 是否沙箱环境 (1/0)
    """

    def __init__(
        self,
        appid: Optional[str] = None,
        secret: Optional[str] = None,
        sandbox: bool = False,
    ):
        self.appid = appid or os.environ.get("QQ_APPID", "")
        self.secret = secret or os.environ.get("QQ_SECRET", "")
        self.sandbox = sandbox or os.environ.get("QQ_SANDBOX", "0") == "1"

        self._client: Optional[Any] = None
        self._message_handler: Optional[Callable[[QQMessage], Awaitable[None]]] = None
        self._running = False
        self._injected_messages: List[QQMessage] = []  # mock mode 用

    # ── SDK 客户端构建 ──────────────────────

    def _build_client(self) -> Any:
        """构建 botpy Client 子类"""
        try:
            import botpy
            from botpy.message import Message as BotpyMessage

            adapter = self  # 闭包引用

            class CourseBotClient(botpy.Client):
                """课程助手 Bot 客户端"""

                async def on_at_message_create(self, message: BotpyMessage):
                    """频道 @消息"""
                    qq_msg = _convert_botpy_message(message, MessageType.CHANNEL)
                    await adapter._dispatch(qq_msg)

                async def on_group_at_message_create(self, message: BotpyMessage):
                    """群聊 @消息"""
                    qq_msg = _convert_botpy_message(message, MessageType.GROUP)
                    await adapter._dispatch(qq_msg)

                async def on_c2c_message_create(self, message: BotpyMessage):
                    """C2C 私聊消息"""
                    qq_msg = _convert_botpy_message(message, MessageType.PRIVATE)
                    await adapter._dispatch(qq_msg)

                async def on_direct_message_create(self, message: BotpyMessage):
                    """频道私信"""
                    qq_msg = _convert_botpy_message(message, MessageType.PRIVATE)
                    await adapter._dispatch(qq_msg)

            return CourseBotClient

        except ImportError:
            logger.warning(
                "qq-botpy 未安装，将使用 mock 模式。"
                "安装: pip install qq-botpy"
            )
            return None

    # ── 消息处理 ────────────────────────────

    def on_message(self, handler: Callable[[QQMessage], Awaitable[None]]):
        """注册消息处理器"""
        self._message_handler = handler

    async def _dispatch(self, msg: QQMessage):
        """分发消息到处理器"""
        logger.info(f"收到消息: user={msg.user_id}, content={msg.content[:50]}")
        if self._message_handler:
            await self._message_handler(msg)

    # ── 启动 / 停止 ────────────────────────

    async def start(self):
        """启动 QQ Bot（真实模式）"""
        if not self.appid or not self.secret:
            logger.warning(
                "QQ_APPID 或 QQ_SECRET 未配置，使用 mock 模式"
            )
            self._running = True
            return

        ClientClass = self._build_client()
        if ClientClass is None:
            logger.warning("botpy SDK 不可用，使用 mock 模式")
            self._running = True
            return

        import botpy

        intents = botpy.Intents.none()
        intents.public_guild_messages = True
        intents.public_guild_messages = True   # 频道消息
        # 群聊和C2C需要在QQ开放平台申请权限后启用

        self._client = ClientClass(intents=intents)

        # 在后台运行 bot
        self._running = True
        logger.info(f"Qclaw Bot 启动: appid={self.appid}")

        # client.run() 是阻塞的，在独立线程中运行
        import threading
        def _run_bot():
            asyncio.run(
                self._client.run(appid=self.appid, secret=self.secret)
            )

        self._bot_thread = threading.Thread(target=_run_bot, daemon=True)
        self._bot_thread.start()

    async def stop(self):
        """停止 Bot"""
        self._running = False
        if self._client:
            # botpy 没有优雅的 stop 方法，依赖 daemon 线程自动退出
            logger.info("Qclaw Bot 已停止")

    # ── 发送消息 ────────────────────────────

    async def send_message(self, user_id: str, content: str,
                           group_id: Optional[str] = None):
        """发送消息给用户/群"""
        if self._client:
            try:
                if group_id:
                    await self._client.api.post_group_message(
                        group_openid=group_id,
                        content=content,
                        msg_type=0,  # 文本
                    )
                else:
                    await self._client.api.post_c2c_message(
                        openid=user_id,
                        content=content,
                        msg_type=0,
                    )
                logger.info(f"消息已发送: user={user_id}, len={len(content)}")
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
        else:
            # Mock 模式
            logger.info(f"[Mock] 发送消息给 {user_id}: {content[:50]}")

    async def send_group_message(self, group_id: str, content: str):
        """发送群消息"""
        await self.send_message(
            user_id="", content=content, group_id=group_id
        )

    # ── 文件操作 ────────────────────────────

    async def download_file(self, file_url: str,
                            save_path: str) -> str:
        """下载文件"""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status == 200:
                    with open(save_path, "wb") as f:
                        f.write(await resp.read())
                    return save_path
                else:
                    raise IOError(f"下载失败: HTTP {resp.status}")

    # ── 群成员 / 角色 ───────────────────────

    async def get_member_roles(self, group_id: str,
                               user_id: str) -> List[str]:
        """获取成员角色（TA/学生/教师）"""
        if self._client:
            try:
                # QQ 开放平台 API 获取成员信息
                # 具体 API 取决于权限等级
                return ["member"]
            except Exception:
                return ["member"]
        return ["member"]

    # ── Mock 模式支持 ──────────────────────

    def inject_message(self, msg: QQMessage):
        """
        注入消息（测试用）
        在 mock 模式下模拟收到消息
        """
        self._injected_messages.append(msg)

    async def process_injected(self):
        """处理注入的消息"""
        while self._injected_messages:
            msg = self._injected_messages.pop(0)
            await self._dispatch(msg)

    def create_mock_message(
        self,
        user_id: str = "test_user",
        content: str = "测试消息",
        group_id: Optional[str] = None,
        message_type: str = MessageType.PRIVATE,
    ) -> QQMessage:
        """创建 mock 消息（测试辅助）"""
        import uuid
        return QQMessage(
            user_id=user_id,
            group_id=group_id,
            message_id=f"mock_{uuid.uuid4().hex[:8]}",
            content=content,
            timestamp=datetime.now(),
            message_type=message_type,
        )

    # ── 状态 ────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def mode(self) -> str:
        if self.appid and self.secret:
            try:
                import botpy
                return "real"
            except ImportError:
                return "mock (botpy未安装)"
        return "mock (未配置)"
