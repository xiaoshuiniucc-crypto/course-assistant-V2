"""
QQ Bot adapter (Qclaw).

Supports:
- channel @ messages
- group @ messages
- c2c private messages
- direct messages
- attachment passthrough
- mock mode for local testing
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional

from contracts.models import MessageType, QQMessage

logger = logging.getLogger("qclaw")


def _convert_botpy_message(msg: Any, msg_type: str = MessageType.PRIVATE) -> QQMessage:
    """Convert a botpy message object into the project's QQMessage."""
    content = getattr(msg, "content", "") or ""
    if content.startswith("<@!"):
        import re

        content = re.sub(r"<@!\d+>\s*", "", content).strip()

    attachments = getattr(msg, "attachments", None) or []
    file_url = None
    file_name = None
    if attachments:
        first_attachment = attachments[0]
        if isinstance(first_attachment, dict):
            file_url = first_attachment.get("url")
            file_name = first_attachment.get("filename") or first_attachment.get("name")
        else:
            file_url = getattr(first_attachment, "url", None)
            file_name = getattr(first_attachment, "filename", None) or getattr(
                first_attachment, "name", None
            )

    author = getattr(msg, "author", {}) or {}
    user_id = (
        author.get("user_openid")
        or author.get("member_openid")
        or author.get("id")
        or "unknown"
    )

    return QQMessage(
        user_id=str(user_id),
        group_id=str(getattr(msg, "group_openid", None) or ""),
        message_id=getattr(msg, "id", ""),
        content=content,
        timestamp=datetime.now(),
        message_type=msg_type,
        file_url=file_url,
        file_name=file_name,
        raw=msg.__dict__ if hasattr(msg, "__dict__") else {},
    )


class QclawAdapter:
    """
    QQ bot adapter.

    Config:
    - QQ_APPID
    - QQ_SECRET
    - QQ_SANDBOX
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
        self._injected_messages: List[QQMessage] = []

    def _build_client(self) -> Any:
        try:
            import botpy
            from botpy.message import Message as BotpyMessage

            adapter = self

            class CourseBotClient(botpy.Client):
                async def on_at_message_create(self, message: BotpyMessage):
                    await adapter._dispatch(
                        _convert_botpy_message(message, MessageType.CHANNEL)
                    )

                async def on_group_at_message_create(self, message: BotpyMessage):
                    await adapter._dispatch(
                        _convert_botpy_message(message, MessageType.GROUP)
                    )

                async def on_c2c_message_create(self, message: BotpyMessage):
                    await adapter._dispatch(
                        _convert_botpy_message(message, MessageType.PRIVATE)
                    )

                async def on_direct_message_create(self, message: BotpyMessage):
                    await adapter._dispatch(
                        _convert_botpy_message(message, MessageType.PRIVATE)
                    )

            return CourseBotClient
        except ImportError:
            logger.warning("qq-botpy is not installed, mock mode will be used.")
            return None

    def on_message(self, handler: Callable[[QQMessage], Awaitable[None]]):
        self._message_handler = handler

    async def _dispatch(self, msg: QQMessage):
        logger.info("Received message: user=%s content=%s", msg.user_id, msg.content[:50])
        if self._message_handler:
            await self._message_handler(msg)

    async def start(self):
        if not self.appid or not self.secret:
            logger.warning("QQ_APPID or QQ_SECRET is missing, using mock mode")
            self._running = True
            return

        client_class = self._build_client()
        if client_class is None:
            logger.warning("botpy SDK unavailable, using mock mode")
            self._running = True
            return

        import botpy

        intents = botpy.Intents.none()
        intents.public_guild_messages = True
        if hasattr(intents, "public_messages"):
            intents.public_messages = True
        if hasattr(intents, "direct_message"):
            intents.direct_message = True

        self._client = client_class(intents=intents)
        self._running = True
        logger.info("Qclaw bot starting: appid=%s sandbox=%s", self.appid, self.sandbox)

        import threading

        def _run_bot():
            asyncio.run(self._client.run(appid=self.appid, secret=self.secret))

        self._bot_thread = threading.Thread(target=_run_bot, daemon=True)
        self._bot_thread.start()

    async def stop(self):
        self._running = False
        if self._client:
            try:
                if hasattr(self._client, "_http") and hasattr(self._client._http, "session"):
                    await self._client._http.session.close()
            except Exception:
                pass
            logger.info("Qclaw bot stopped")

    async def send_message(
        self, user_id: str, content: str, group_id: Optional[str] = None
    ):
        if self._client:
            try:
                if group_id:
                    await self._client.api.post_group_message(
                        group_openid=group_id,
                        content=content,
                        msg_type=0,
                    )
                else:
                    await self._client.api.post_c2c_message(
                        openid=user_id,
                        content=content,
                        msg_type=0,
                    )
                logger.info("Message sent: user=%s len=%s", user_id, len(content))
            except Exception as e:
                logger.error("Failed to send message: %s", e)
        else:
            logger.info("[Mock] send to %s: %s", user_id, content[:50])

    async def send_group_message(self, group_id: str, content: str):
        await self.send_message(user_id="", content=content, group_id=group_id)

    async def download_file(
        self,
        file_url: str,
        save_path: str,
        timeout: int = 60,
        max_size: int = 50 * 1024 * 1024,
    ) -> str:
        import aiohttp

        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(file_url) as resp:
                if resp.status == 200:
                    content_length = resp.content_length
                    if content_length and content_length > max_size:
                        raise IOError(
                            f"File too large: {content_length / 1024 / 1024:.1f}MB "
                            f"(limit: {max_size / 1024 / 1024:.0f}MB)"
                        )

                    data = b""
                    async for chunk in resp.content.iter_chunked(8192):
                        data += chunk
                        if len(data) > max_size:
                            raise IOError(
                                f"File exceeds size limit ({max_size / 1024 / 1024:.0f}MB)"
                            )

                    with open(save_path, "wb") as f:
                        f.write(data)
                    return save_path
                raise IOError(f"Download failed: HTTP {resp.status}")

    async def get_member_roles(self, group_id: str, user_id: str) -> List[str]:
        if self._client:
            try:
                member_info = await self._client.api.get_group_member(
                    group_openid=group_id,
                    member_openid=user_id,
                )
                roles = []
                role_name = getattr(member_info, "role", None)
                if isinstance(role_name, str):
                    roles.append(role_name)
                elif isinstance(role_name, list):
                    roles.extend(role_name)

                mapped = []
                for role in roles:
                    normalized = role.lower() if isinstance(role, str) else ""
                    if normalized in ("owner", "admin"):
                        mapped.append("teacher")
                    else:
                        mapped.append("student")
                if mapped:
                    return sorted(set(mapped))
            except Exception as e:
                logger.warning("Failed to read real member roles, fallback to local inference: %s", e)

        roles = self._infer_roles_from_identity(group_id, user_id)
        if roles:
            return roles

        role_map_str = os.environ.get("QQ_ROLE_MAP", "")
        if role_map_str:
            for entry in role_map_str.split(","):
                if ":" in entry:
                    uid, role = entry.strip().split(":", 1)
                    if uid == user_id:
                        return [role.strip()]
        return ["member"]

    def _infer_roles_from_identity(self, group_id: str, user_id: str) -> List[str]:
        configured_teacher_ids = {
            item.strip()
            for item in os.environ.get("QQ_TEACHER_IDS", "").split(",")
            if item.strip()
        }
        configured_ta_ids = {
            item.strip()
            for item in os.environ.get("QQ_TA_IDS", "").split(",")
            if item.strip()
        }

        roles = {"member"}
        normalized_user_id = user_id.lower()
        normalized_group_id = (group_id or "").lower()

        if user_id in configured_teacher_ids:
            roles.update({"teacher", "ta"})
        if user_id in configured_ta_ids:
            roles.add("ta")

        teacher_markers = ("teacher", "prof", "lecturer", "老师")
        ta_markers = ("ta", "assistant", "助教")

        if any(marker in normalized_user_id for marker in teacher_markers):
            roles.update({"teacher", "ta"})
        if any(marker in normalized_user_id for marker in ta_markers):
            roles.add("ta")
        if "teacher" in normalized_group_id:
            roles.update({"teacher", "ta"})
        if "ta" in normalized_group_id:
            roles.add("ta")

        return sorted(roles)

    def inject_message(self, msg: QQMessage):
        self._injected_messages.append(msg)

    async def process_injected(self):
        while self._injected_messages:
            msg = self._injected_messages.pop(0)
            await self._dispatch(msg)

    def create_mock_message(
        self,
        user_id: str = "test_user",
        content: str = "测试消息",
        group_id: Optional[str] = None,
        message_type: str = MessageType.PRIVATE,
        file_url: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> QQMessage:
        import uuid

        return QQMessage(
            user_id=user_id,
            group_id=group_id,
            message_id=f"mock_{uuid.uuid4().hex[:8]}",
            content=content,
            timestamp=datetime.now(),
            message_type=message_type,
            file_url=file_url,
            file_name=file_name,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def mode(self) -> str:
        if self.appid and self.secret:
            try:
                import botpy  # noqa: F401

                return "real"
            except ImportError:
                return "mock (botpy not installed)"
        return "mock (not configured)"
