"""Ephemeral, mutually checked chat channels. No message is written to disk.

The receiver hosts the channel. The initiator polls replies so neither side
needs an additional listening port. Secrets, certificates and source IP bind
requests to a single channel; SAS comparison authenticates discovery pins.
"""

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .discovery import HEX32, HEX64, Peer, device_name
from .peer import comparison_code, peer_request


def message_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 4096:
        raise ValueError("消息不能为空，且最多 4 KiB")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("消息包含无效控制字符")
    return value


@dataclass
class Channel:
    id: str
    secret: str
    peer: Peer
    code: str
    expires: float
    status: str = "pending"
    messages: deque = field(default_factory=lambda: deque(maxlen=200))
    sequence: int = 0
    attempts: deque = field(default_factory=deque)


class ChatProtocol:
    def __init__(self, node):
        self.node = node
        self.channels = {}
        self.attempts = {}
        self.lock = threading.RLock()

    def sweep(self):
        with self.lock:
            for channel in list(self.channels.values()):
                if channel.expires < time.monotonic():
                    self.channels.pop(channel.id)
                    self.node.emit("chat_end", id=channel.id, text="聊天会话已过期，请重新连接")

    def decide(self, identifier, accepted):
        with self.lock:
            channel = self.channels.get(identifier)
            if not channel or channel.status != "pending" or channel.expires < time.monotonic():
                raise ValueError("聊天请求已过期")
            channel.status = "accepted" if accepted else "rejected"
            channel.expires = time.monotonic() + (3600 if accepted else 10)
            return channel

    def close(self, identifier):
        with self.lock:
            channel = self.channels.get(identifier)
            if channel:
                channel.status = "closed"
                channel.expires = time.monotonic() + 10

    def append(self, identifier, text, message_id, author):
        text = message_text(text)
        if not isinstance(message_id, str) or not HEX32.fullmatch(message_id):
            raise ValueError("消息标识无效")
        with self.lock:
            channel = self.channels.get(identifier)
            if not channel or channel.status != "active" or channel.expires < time.monotonic():
                raise ValueError("聊天未获确认或已结束")
            # Idempotent retries within the bounded retained message window.
            for item in channel.messages:
                if item["id"] == message_id:
                    if item["text"] != text or item["author"] != author:
                        raise ValueError("消息标识重复")
                    return item
            now = time.monotonic()
            while channel.attempts and now - channel.attempts[0] > 60:
                channel.attempts.popleft()
            if len(channel.attempts) >= 120:
                raise ValueError("消息发送过快，请稍后再试")
            channel.attempts.append(now)
            channel.sequence += 1
            item = {"seq": channel.sequence, "id": message_id, "text": text, "author": author}
            channel.messages.append(item)
            if author == "initiator":
                self.node.emit("chat_message", channel_id=identifier, peer=channel.peer, **item)
            return item

    def authenticated(self, handler, identifier):
        channel = self.channels.get(identifier)
        if (
            not channel
            or channel.expires < time.monotonic()
            or handler.client_address[0] != channel.peer.host
            or not secrets.compare_digest(channel.secret, handler.headers.get("X-Offer-Secret", ""))
        ):
            raise ValueError("聊天未授权或已过期")
        return channel

    def get(self, handler, path):
        self.sweep()
        parts = path.split("/")
        try:
            with self.lock:
                if len(parts) != 5 or not HEX32.fullmatch(parts[3]) or not parts[4].isdigit():
                    raise ValueError("聊天地址无效")
                channel = self.authenticated(handler, parts[3])
                cursor = int(parts[4])
                messages = (
                    [m for m in channel.messages if m["seq"] > cursor]
                    if channel.status == "active"
                    else []
                )
                # A page stays below peer_request's 64 KiB response limit.
                handler.respond(
                    200,
                    {
                        "status": channel.status,
                        "messages": messages[:4],
                        "oldest": channel.messages[0]["seq"] if channel.messages else 0,
                    },
                )
        except (ValueError, TypeError) as error:
            handler.respond(403, {"error": str(error)})

    def post(self, handler, path, payload):
        self.sweep()
        if path == "/peer/chat/offer":
            self.offer(handler, payload)
            return
        parts = path.split("/")
        with self.lock:
            if len(parts) != 5 or not HEX32.fullmatch(parts[3]):
                raise ValueError("聊天地址无效")
            channel = self.authenticated(handler, parts[3])
            if parts[4] == "activate":
                if channel.status != "accepted":
                    raise ValueError("聊天尚未由接收方确认")
                channel.status = "active"
                self.node.emit("chat_active", channel=channel)
                handler.respond(200, {})
            elif parts[4] == "send":
                item = self.append(
                    channel.id, payload.get("text"), payload.get("message_id"), "initiator"
                )
                handler.respond(200, {"id": item["id"]})
            elif parts[4] == "close":
                self.close(channel.id)
                self.node.emit("chat_end", id=channel.id, text="对方已结束聊天")
                handler.respond(200, {})
            else:
                raise ValueError("聊天接口不存在")

    def offer(self, handler, payload):
        ip, now = handler.client_address[0], time.monotonic()
        with self.lock:
            self.attempts = {
                k: [t for t in v if now - t < 60]
                for k, v in self.attempts.items()
                if any(now - t < 60 for t in v)
            }
            attempts = self.attempts.setdefault(ip, [])
            if len(attempts) >= 5 or len(self.attempts) > 128 or len(self.channels) >= 16:
                raise ValueError("聊天请求过多，请稍后再试")
            attempts.append(now)
            if any(c.status == "pending" for c in self.channels.values()):
                raise ValueError("对方正在确认聊天请求")
        identifier, fingerprint, nonce, port = (
            payload.get(k) for k in ("id", "fingerprint", "nonce", "port")
        )
        if (
            not isinstance(identifier, str)
            or not HEX32.fullmatch(identifier)
            or not isinstance(fingerprint, str)
            or not HEX64.fullmatch(fingerprint)
            or not isinstance(nonce, str)
            or not HEX64.fullmatch(nonce)
            or type(port) is not int
            or not 1024 <= port <= 65535
        ):
            raise ValueError("聊天设备信息无效")
        peer = Peer(identifier, "待验证", ip, port, fingerprint, now)
        hello = peer_request(peer, "GET", "/peer/hello", source=self.node.host, timeout=3)
        if hello.get("id") != identifier or not hello.get("chat"):
            raise ValueError("对方不支持聊天，请升级邻传")
        peer = Peer(identifier, device_name(hello.get("name")), ip, port, fingerprint, now)
        cid = secrets.token_hex(16)
        channel = Channel(
            cid,
            secrets.token_urlsafe(32),
            peer,
            comparison_code(fingerprint, self.node.fingerprint, nonce, cid),
            now + 120,
        )
        with self.lock:
            if (
                self.node.closed.is_set()
                or len(self.channels) >= 16
                or any(c.status == "pending" for c in self.channels.values())
            ):
                raise ValueError("对方正在确认聊天请求")
            self.channels[cid] = channel
        self.node.emit("chat_request", channel=channel)
        handler.respond(
            201,
            {
                "id": cid,
                "secret": channel.secret,
                "code": channel.code,
                "receiver_id": self.node.id,
            },
        )
