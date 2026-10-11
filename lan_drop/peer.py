"""Desktop peer transfer: pin certificates, compare codes, authorize each batch."""

from __future__ import annotations

import hashlib
import http.client
import json
import queue
import secrets
import shutil
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import HEX32, HEX64, Discovery, Peer, device_name
from .server import MAX_FILE, create_server, is_lan, safe_name


def comparison_code(sender_fp, receiver_fp, nonce, offer_id):
    digest = (
        hashlib.sha256((sender_fp + receiver_fp + nonce + offer_id).encode("ascii"))
        .hexdigest()[:16]
        .upper()
    )
    return "-".join(digest[i : i + 4] for i in range(0, 16, 4))


class PinnedConnection(http.client.HTTPSConnection):
    """Untrusted local certificates are accepted only with an exact SHA-256 pin.

    The discovery pin is provisional; both users must additionally compare the
    batch's 64-bit code to authenticate the two endpoints before any file moves.
    """

    def __init__(self, peer: Peer, source: str | None = None, timeout=30):
        if not is_lan(peer.host) or not HEX64.fullmatch(peer.fingerprint):
            raise ValueError("无效的局域网设备")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.fingerprint = peer.fingerprint
        super().__init__(
            peer.host,
            peer.port,
            context=context,
            timeout=timeout,
            source_address=(source, 0) if source else None,
        )

    def connect(self):
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not secrets.compare_digest(actual, self.fingerprint):
            self.close()
            raise ssl.SSLError("设备证书已变化，已阻止传输；请刷新设备后重新核对验证码")


def peer_request(peer, method, path, data=None, secret=None, source=None, timeout=10):
    body = json.dumps(data).encode() if data is not None else b""
    headers = {"Origin": f"https://{peer.host}:{peer.port}", "Content-Type": "application/json"}
    if secret:
        headers["X-Offer-Secret"] = secret
    connection = PinnedConnection(peer, source, timeout)
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        # Every control message is bounded independently of peer trust.
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("设备响应过大")
        result = json.loads(raw)
        if response.status >= 400:
            raise ValueError(result.get("error", "设备拒绝请求"))
        return result
    finally:
        connection.close()


def describe_files(paths, cancelled, progress):
    if not 1 <= len(paths) <= 50:
        raise ValueError("每次可发送 1 至 50 个文件")
    result = []
    for path in paths:
        path = Path(path)
        name = safe_name(path.name)
        size = path.stat().st_size
        if not path.is_file() or size > MAX_FILE:
            raise ValueError(f"{name} 不是普通文件或超过 2 GiB")
        digest, total = hashlib.sha256(), 0
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                if cancelled.is_set():
                    raise InterruptedError("发送已取消")
                total += len(block)
                if total > MAX_FILE:
                    raise ValueError("文件超过 2 GiB")
                digest.update(block)
                progress(name, total, size)
        if total != size:
            raise ValueError(f"{name} 在校验期间发生变化，请重新发送")
        result.append({"name": name, "size": size, "sha256": digest.hexdigest()})
    if sum(item["size"] for item in result) > 20 * 1024**3:
        raise ValueError("单次发送总量最多 20 GiB")
    return result


def validate_files(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError("文件数量无效")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("文件信息无效")
        name = safe_name(item.get("name", ""))
        size, digest = item.get("size"), item.get("sha256")
        if (
            type(size) is not int
            or not 0 <= size <= MAX_FILE
            or not isinstance(digest, str)
            or not HEX64.fullmatch(digest)
        ):
            raise ValueError("文件大小或校验值无效")
        result.append({"name": name, "size": size, "sha256": digest})
    if sum(item["size"] for item in result) > 20 * 1024**3:
        raise ValueError("单批文件过大")
    return result


@dataclass
class Incoming:
    id: str
    secret: str
    sender: Peer
    files: list[dict]
    code: str
    expires: float
    status: str = "pending"
    token: str = ""
    complete: set[int] = field(default_factory=set)
    active: set[int] = field(default_factory=set)


class PeerProtocol:
    def __init__(self, node):
        self.node = node
        self.offers: dict[str, Incoming] = {}
        self.attempts: dict[str, list[float]] = {}
        self.lock = threading.RLock()

    def sweep(self):
        with self.lock:
            now = time.monotonic()
            for offer in list(self.offers.values()):
                if offer.expires < now:
                    self.offers.pop(offer.id, None)
                    if offer.status in {"pending", "accepted"}:
                        offer.status = "expired"
                        self.node.emit("incoming_end", id=offer.id, text="接收请求已超时")

    def decide(self, identifier, accepted):
        with self.lock:
            offer = self.offers.get(identifier)
            if not offer or offer.status != "pending" or offer.expires < time.monotonic():
                raise ValueError("请求已过期")
            total = sum(item["size"] for item in offer.files)
            if accepted and shutil.disk_usage(self.node.received).free < total + 128 * 1024**2:
                raise ValueError("本机磁盘空间不足")
            offer.status = "accepted" if accepted else "rejected"
            offer.expires = time.monotonic() + (3600 if accepted else 60)
            if accepted:
                offer.token = secrets.token_urlsafe(32)
            self.node.emit("incoming_decided", id=offer.id, accepted=accepted)

    def authenticated_offer(self, handler, identifier):
        # Only the sender holding the per-offer secret can query or cancel it.
        with self.lock:
            offer = self.offers.get(identifier)
            secret = handler.headers.get("X-Offer-Secret", "")
            if (
                not offer
                or offer.expires < time.monotonic()
                or offer.sender.host != handler.client_address[0]
                or not secrets.compare_digest(offer.secret, secret)
            ):
                raise ValueError("请求不存在或未授权")
            return offer

    def abort(self, identifier):
        with self.lock:
            offer = self.offers.get(identifier)
            if not offer or offer.status not in {"pending", "accepted"}:
                raise ValueError("请求已结束")
            offer.status = "cancelled"
            offer.token = ""
            self.node.emit("incoming_end", id=identifier, text="已停止接收")

    def get(self, handler, path):
        self.sweep()
        if path == "/peer/hello":
            handler.respond(200, {"app": "lindrop", "protocol": 2, **self.node.identity()})
            return
        try:
            identifier = path.removeprefix("/peer/offers/")
            if not path.startswith("/peer/offers/") or not HEX32.fullmatch(identifier):
                raise ValueError("接口不存在")
            offer = self.authenticated_offer(handler, identifier)
            with self.lock:
                handler.respond(
                    200,
                    {
                        "status": offer.status,
                        "token": offer.token,
                        "complete": sorted(offer.complete),
                    },
                )
        except ValueError as error:
            handler.respond(403, {"error": str(error)})

    def post(self, handler, path):
        self.sweep()
        if path.startswith("/peer/upload/"):
            self.upload(handler, path)
            return
        try:
            length = handler.body_length(32768)
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("无效控制消息")
            if path == "/peer/offer":
                self.offer(handler, payload)
            elif path.startswith("/peer/cancel/"):
                offer = self.authenticated_offer(handler, path.removeprefix("/peer/cancel/"))
                with self.lock:
                    offer.status = "cancelled"
                    offer.token = ""
                self.node.emit("incoming_end", id=offer.id, text="对方已取消发送")
                handler.respond(200, {})
            else:
                handler.respond(404, {"error": "接口不存在"})
        except (ValueError, TypeError, KeyError, UnicodeError, OSError) as error:
            handler.respond(400, {"error": str(error) or "设备请求失败"})

    def offer(self, handler, payload):
        ip = handler.client_address[0]
        now = time.monotonic()
        with self.lock:
            self.attempts = {
                key: [t for t in times if now - t < 60]
                for key, times in self.attempts.items()
                if any(now - t < 60 for t in times)
            }
            attempts = self.attempts.setdefault(ip, [])
            if len(attempts) >= 5 or len(self.attempts) > 128:
                raise ValueError("接收请求过于频繁，请稍后再试")
            attempts.append(now)
            if len(self.offers) >= 16:
                raise ValueError("请求数量已达上限")
            if any(o.status in {"pending", "accepted"} for o in self.offers.values()):
                raise ValueError("对方正在处理其他接收请求，请稍后再试")
        files = validate_files(payload.get("files"))
        identifier, fingerprint = payload.get("id", ""), payload.get("fingerprint", "")
        nonce, port = payload.get("nonce", ""), payload.get("port")
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
            raise ValueError("发送端信息无效")
        sender = Peer(identifier, "待验证", ip, port, fingerprint, now)
        hello = peer_request(sender, "GET", "/peer/hello", source=self.node.host, timeout=3)
        if hello.get("id") != identifier or hello.get("protocol") != 2:
            raise ValueError("发送设备身份不一致")
        sender = Peer(identifier, device_name(hello.get("name")), ip, port, fingerprint, now)
        offer_id = secrets.token_hex(16)
        code = comparison_code(fingerprint, self.node.fingerprint, nonce, offer_id)
        offer = Incoming(offer_id, secrets.token_urlsafe(32), sender, files, code, now + 120)
        with self.lock:
            # Reverse probe occurs outside the lock; check again to serialize requests.
            if self.node.closed.is_set() or any(
                o.status in {"pending", "accepted"} for o in self.offers.values()
            ):
                raise ValueError("对方正在处理其他请求")
            self.offers[offer_id] = offer
        self.node.emit("incoming", id=offer.id, name=sender.name, host=ip, files=files, code=code)
        handler.respond(
            201, {"id": offer.id, "secret": offer.secret, "code": code, "receiver_id": self.node.id}
        )

    def upload(self, handler, path):
        temporary = None
        offer, index = None, None
        claimed = False
        try:
            components = path.split("/")
            if len(components) != 5 or not components[4].isdigit():
                raise ValueError("传输地址无效")
            identifier, index = components[3], int(components[4])
            with self.lock:
                offer = self.offers.get(identifier)
                token = handler.headers.get("X-Transfer-Token", "")
                if (
                    not offer
                    or offer.status != "accepted"
                    or offer.expires < time.monotonic()
                    or offer.sender.host != handler.client_address[0]
                    or not secrets.compare_digest(offer.token, token)
                    or not 0 <= index < len(offer.files)
                    or index in offer.complete
                    or index in offer.active
                ):
                    raise ValueError("接收未获确认、已过期或文件已发送")
                item = offer.files[index]
                if handler.body_length(MAX_FILE) != item["size"]:
                    raise ValueError("文件大小与确认的信息不一致")
                offer.active.add(index)
                claimed = True
            if shutil.disk_usage(self.node.received).free < item["size"] + 128 * 1024**2:
                raise ValueError("磁盘空间不足")
            prefix = secrets.token_hex(16)
            temporary = self.node.received / (prefix + ".part")
            target = self.node.received / (prefix + "_" + item["name"])
            digest, remaining = hashlib.sha256(), item["size"]
            last = 0
            with temporary.open("xb") as output:
                while remaining:
                    if self.node.closed.is_set() or offer.status != "accepted":
                        raise InterruptedError("接收已取消")
                    if time.monotonic() > offer.expires:
                        raise TimeoutError("传输超时")
                    block = handler.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ConnectionError("连接中断")
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
                    if time.monotonic() - last > 0.1:
                        self.node.emit(
                            "receive_progress",
                            id=offer.id,
                            name=item["name"],
                            done=item["size"] - remaining,
                            total=item["size"],
                        )
                        last = time.monotonic()
            if not secrets.compare_digest(digest.hexdigest(), item["sha256"]):
                raise ValueError("文件校验失败，未保存文件，请重新发送")
            with self.lock:
                if offer.status != "accepted" or self.node.closed.is_set():
                    raise InterruptedError("接收已取消")
                temporary.replace(target)
                offer.complete.add(index)
                if len(offer.complete) == len(offer.files):
                    offer.status = "complete"
                    offer.token = ""
                    offer.expires = time.monotonic() + 60
            self.node.emit(
                "received",
                id=offer.id,
                name=item["name"],
                path=str(target),
                peer=offer.sender.name,
                finished=offer.status == "complete",
            )
            handler.respond(201, {"sha256": digest.hexdigest()})
        except (OSError, ValueError, ConnectionError) as error:
            if temporary:
                temporary.unlink(missing_ok=True)
            if claimed:
                with self.lock:
                    offer.status = "cancelled"
                    offer.token = ""
                    offer.expires = time.monotonic() + 60
                self.node.emit("receive_error", id=offer.id, text=str(error))
            handler.respond(400, {"error": str(error) or "传输中断"})
        finally:
            if offer is not None and claimed:
                with self.lock:
                    offer.active.discard(index)


class PeerNode:
    def __init__(self, host, port, directory, name=None, discover=True):
        self.host, self.directory = host, Path(directory)
        self.name = device_name(name or socket.gethostname())
        self.id = secrets.token_hex(16)
        self.events = queue.Queue()
        self.closed = threading.Event()
        self.server, fingerprint = create_server(host, port, self.directory)
        self.fingerprint = fingerprint.replace(":", "").lower()
        self.port = self.server.server_port
        self.received = self.server.state.received
        self.protocol = PeerProtocol(self)
        self.server.peer_protocol = self.protocol
        self.discovery = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if discover:
            try:
                self.discovery = Discovery(host, self.identity, self.emit)
            except OSError as error:
                self.emit("notice", text=f"自动发现不可用：{error}；可使用手动连接")

    def identity(self):
        return {
            "id": self.id,
            "name": self.name,
            "port": self.port,
            "fingerprint": self.fingerprint,
        }

    def as_peer(self):
        return Peer(self.id, self.name, self.host, self.port, self.fingerprint, time.monotonic())

    def emit(self, kind, **payload):
        self.events.put({"kind": kind, **payload})

    def close(self):
        self.closed.set()
        with self.protocol.lock:
            for offer in self.protocol.offers.values():
                offer.status = "cancelled"
                offer.token = ""
        if self.discovery:
            self.discovery.close()
        self.server.stop()

    def manual_peer(self, host, port=8765):
        if not is_lan(host) or not 1024 <= port <= 65535:
            raise ValueError("请输入局域网 IPv4 与有效端口")
        # Learn a provisional pin through TLS; human code comparison is mandatory later.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.create_connection(
            (host, port), timeout=3, source_address=(self.host, 0)
        ) as connection:
            with context.wrap_socket(connection, server_hostname=host) as secure:
                fingerprint = hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest()
        candidate = Peer("0" * 32, "", host, port, fingerprint, time.monotonic())
        hello = peer_request(candidate, "GET", "/peer/hello", source=self.host)
        if hello.get("app") != "lindrop" or hello.get("protocol") != 2:
            raise ValueError("对方不是邻传桌面版")
        identifier = hello.get("id", "")
        if not isinstance(identifier, str) or not HEX32.fullmatch(identifier):
            raise ValueError("设备身份无效")
        return Peer(
            identifier, device_name(hello.get("name")), host, port, fingerprint, time.monotonic()
        )

    def begin(self, peer, files):
        nonce = secrets.token_hex(32)
        result = peer_request(
            peer,
            "POST",
            "/peer/offer",
            {
                **self.identity(),
                "files": files,
                "nonce": nonce,
            },
            source=self.host,
        )
        identifier, secret = result.get("id", ""), result.get("secret", "")
        if (
            not isinstance(identifier, str)
            or not HEX32.fullmatch(identifier)
            or not isinstance(secret, str)
            or not 30 <= len(secret) <= 64
            or result.get("receiver_id") != peer.id
        ):
            raise ValueError("对方设备身份或接收请求无效")
        code = comparison_code(self.fingerprint, peer.fingerprint, nonce, identifier)
        if result.get("code") != code:
            raise ValueError("验证信息不一致，已阻止传输")
        return {"id": identifier, "secret": secret, "code": code}

    def cancel(self, peer, offer):
        return peer_request(
            peer, "POST", "/peer/cancel/" + offer["id"], {}, offer["secret"], self.host
        )

    def wait_accepted(self, peer, offer, cancelled):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.closed.is_set() or cancelled.is_set():
                raise InterruptedError("发送已取消")
            result = peer_request(
                peer, "GET", "/peer/offers/" + offer["id"], secret=offer["secret"], source=self.host
            )
            if result.get("status") == "accepted":
                token = result.get("token")
                if not isinstance(token, str) or not 30 <= len(token) <= 64:
                    raise ValueError("接收授权无效")
                return token
            if result.get("status") != "pending":
                raise ValueError("对方已拒绝、取消或请求已过期")
            cancelled.wait(0.4)
        raise TimeoutError("等待接收确认超时，请重新发送")

    def send_file(self, peer, offer, token, index, path, item, cancelled, progress):
        connection = PinnedConnection(peer, self.host)
        try:
            with Path(path).open("rb") as source:
                connection.putrequest("POST", f"/peer/upload/{offer['id']}/{index}")
                connection.putheader("Origin", f"https://{peer.host}:{peer.port}")
                connection.putheader("X-Transfer-Token", token)
                connection.putheader("Content-Length", str(item["size"]))
                connection.endheaders()
                remaining = item["size"]
                started = time.monotonic()
                while remaining:
                    if self.closed.is_set() or cancelled.is_set():
                        raise InterruptedError("发送已取消")
                    if time.monotonic() - started > 3600:
                        raise TimeoutError("发送超时")
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("文件已变化，请重新发送")
                    connection.send(block)
                    remaining -= len(block)
                    progress(item["name"], item["size"] - remaining, item["size"])
                response = connection.getresponse()
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("设备响应过大")
                result = json.loads(raw)
                if response.status != 201:
                    raise ValueError(result.get("error", "对方保存失败"))
                if result.get("sha256") != item["sha256"]:
                    raise ValueError("文件校验值不一致")
        finally:
            connection.close()
