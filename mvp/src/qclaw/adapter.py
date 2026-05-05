"""
M01 — Qclaw 接入层（Mock 版本）
生产环境替换为 Qclaw SDK 调用，MVP 阶段用控制台/内存模拟。

核心接口：
- on_group_message(handler)     注册群消息回调
- on_private_message(handler)   注册私聊回调
- on_channel_message(handler)   注册频道回调
- send_private(qq, text)        私聊发送
- send_group(group_id, text)    群消息发送
- send_channel(channel_id, text) 频道消息发送
- download_file(file_id) -> path 文件下载
- get_member_role(group_id, qq) 获取成员角色
"""

from dataclasses import dataclass, field
from typing import Callable, Optional
from pathlib import Path


@dataclass
class QQMessage:
    """统一消息结构。"""
    msg_type: str          # "group" / "private" / "channel"
    sender_qq: str         # 发送者 QQ 号
    group_id: Optional[str] = None    # 群 ID（群消息时）
    channel_id: Optional[str] = None  # 频道 ID（频道消息时）
    text: str = ""         # 文本内容
    file_id: Optional[str] = None     # 文件 ID（文件消息时）
    file_name: Optional[str] = None   # 文件名
    is_file: bool = False            # 是否是文件消息
    tag: Optional[str] = None        # 消息标签（如 #课件, #评分细则）


class QclawAdapter:
    """
    Mock 版 Qclaw 适配器。
    - 消息发送记录到内存列表（可用于测试断言）
    - 文件下载模拟为复制本地文件
    - 成员角色从预设字典查询
    """

    def __init__(self):
        self._group_handlers: list[Callable] = []
        self._private_handlers: list[Callable] = []
        self._channel_handlers: list[Callable] = []
        self._sent_messages: list[dict] = []    # 已发送消息记录
        self._member_roles: dict[str, dict[str, str]] = {}  # {group_id: {qq: role}}
        self._file_store: dict[str, str] = {}   # {file_id: local_path}

    # ── 回调注册 ──────────────────────────────────────────

    def on_group_message(self, handler: Callable[[QQMessage], None]) -> None:
        """注册群消息回调。"""
        self._group_handlers.append(handler)

    def on_private_message(self, handler: Callable[[QQMessage], None]) -> None:
        """注册私聊回调。"""
        self._private_handlers.append(handler)

    def on_channel_message(self, handler: Callable[[QQMessage], None]) -> None:
        """注册频道回调。"""
        self._channel_handlers.append(handler)

    # ── 消息发送 ──────────────────────────────────────────

    def send_private(self, qq: str, text: str) -> None:
        """私聊发送消息。"""
        msg = {"type": "private", "target": qq, "text": text}
        self._sent_messages.append(msg)
        print(f"  📩 [私聊→{qq}] {text[:100]}{'...' if len(text) > 100 else ''}")

    def send_group(self, group_id: str, text: str) -> None:
        """群消息发送。"""
        msg = {"type": "group", "target": group_id, "text": text}
        self._sent_messages.append(msg)
        print(f"  📢 [群→{group_id}] {text[:100]}{'...' if len(text) > 100 else ''}")

    def send_channel(self, channel_id: str, text: str) -> None:
        """频道消息发送。"""
        msg = {"type": "channel", "target": channel_id, "text": text}
        self._sent_messages.append(msg)
        print(f"  📣 [频道→{channel_id}] {text[:100]}{'...' if len(text) > 100 else ''}")

    # ── 文件操作 ──────────────────────────────────────────

    def download_file(self, file_id: str) -> str:
        """模拟文件下载，返回本地文件路径。"""
        if file_id in self._file_store:
            return self._file_store[file_id]
        # Mock: 返回一个空临时文件
        from tempfile import NamedTemporaryFile
        tmp = NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
        tmp.write(f"[Mock file content for {file_id}]")
        tmp.close()
        self._file_store[file_id] = tmp.name
        return tmp.name

    def register_file(self, file_id: str, local_path: str) -> None:
        """注册文件映射（测试用，让 download_file 返回真实路径）。"""
        self._file_store[file_id] = local_path

    # ── 成员角色 ──────────────────────────────────────────

    def set_member_role(self, group_id: str, qq: str, role: str) -> None:
        """设置成员角色（测试用）。"""
        if group_id not in self._member_roles:
            self._member_roles[group_id] = {}
        self._member_roles[group_id][qq] = role

    def get_member_role(self, group_id: str, qq: str) -> str:
        """获取成员角色：teacher / ta / student。"""
        return self._member_roles.get(group_id, {}).get(qq, "student")

    def get_group_members(self, group_id: str) -> list[dict]:
        """获取群成员列表。"""
        members = []
        for qq, role in self._member_roles.get(group_id, {}).items():
            members.append({"qq": qq, "role": role})
        return members

    # ── 消息注入（Mock 专用）──────────────────────────────

    def inject_group_message(self, sender_qq: str, text: str = "",
                              file_id: Optional[str] = None,
                              file_name: Optional[str] = None,
                              tag: Optional[str] = None) -> None:
        """模拟收到群消息（测试用）。"""
        msg = QQMessage(
            msg_type="group", sender_qq=sender_qq,
            group_id="default_group", text=text,
            file_id=file_id, file_name=file_name,
            is_file=file_id is not None, tag=tag,
        )
        for handler in self._group_handlers:
            handler(msg)

    def inject_private_message(self, sender_qq: str, text: str = "",
                                file_id: Optional[str] = None,
                                file_name: Optional[str] = None,
                                tag: Optional[str] = None) -> None:
        """模拟收到私聊消息（测试用）。"""
        msg = QQMessage(
            msg_type="private", sender_qq=sender_qq,
            text=text, file_id=file_id, file_name=file_name,
            is_file=file_id is not None, tag=tag,
        )
        for handler in self._private_handlers:
            handler(msg)

    def inject_channel_message(self, sender_qq: str, text: str) -> None:
        """模拟收到频道消息（测试用）。"""
        msg = QQMessage(
            msg_type="channel", sender_qq=sender_qq,
            channel_id="ta_channel", text=text,
        )
        for handler in self._channel_handlers:
            handler(msg)

    # ── 查询已发送消息（测试用）────────────────────────────

    def get_sent_messages(self, msg_type: Optional[str] = None,
                          target: Optional[str] = None) -> list[dict]:
        """查询已发送消息，可按类型和目标过滤。"""
        result = self._sent_messages
        if msg_type:
            result = [m for m in result if m["type"] == msg_type]
        if target:
            result = [m for m in result if m["target"] == target]
        return result

    def clear_sent_messages(self) -> None:
        """清空已发送消息记录。"""
        self._sent_messages.clear()
