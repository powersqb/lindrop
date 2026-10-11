"""Bounded HTTPS service; desktop transfers use explicit receive authorization."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import quote, unquote, urlsplit

from . import __version__

WEB = Path(__file__).parent / "web"
NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
)
MAX_FILE = 2 * 1024**3


def is_lan(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in network for network in NETWORKS)
    except ValueError:
        return False


def local_addresses() -> list[str]:
    addresses = {
        entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    }
    return sorted(ip for ip in addresses if is_lan(ip) and not ip.startswith("127."))


def safe_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    if (
        not name
        or len(name.encode("utf-8")) > 200
        or name in {".", ".."}
        or any(c in name for c in '/\\:<>"|?*')
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or name[-1:] in {".", " "}
        or name.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }
    ):
        raise ValueError("文件名不合法，请重命名后再传输")
    return name


def prepare_certificate(directory: Path, host: str) -> tuple[Path, Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Protect the private key and received files from other local accounts.
        identity = subprocess.check_output(
            ["whoami"], text=True, creationflags=subprocess.CREATE_NO_WINDOW
        ).strip()
        subprocess.run(
            [
                "icacls",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
            ],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        directory.chmod(0o700)
    cert, key = directory / "certificate.pem", directory / "private-key.pem"
    # A new certificate each run binds its fingerprint to this pairing session.
    candidates = [
        str(Path(getattr(sys, "_MEIPASS", WEB.parent)) / "openssl" / "openssl.exe"),
        shutil.which("openssl"),
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
        r"C:\Program Files\Git\usr\bin\openssl.exe",
    ]
    executable = next((p for p in candidates if p and Path(p).is_file()), None)
    if not executable:
        raise RuntimeError("未找到 OpenSSL。请安装 Git for Windows，或将 openssl 加入 PATH。")
    config = directory / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name=req_dn\n[req_dn]\n", encoding="ascii")
    subprocess.run(
        [
            executable,
            "req",
            "-config",
            config.name,
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "7",
            "-subj",
            "/CN=LinDrop local transfer",
            "-addext",
            f"subjectAltName=IP:{host},IP:127.0.0.1",
            "-keyout",
            key.name,
            "-out",
            cert.name,
        ],
        check=True,
        capture_output=True,
        cwd=directory,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    key.chmod(0o600)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text("ascii"))
    fingerprint = hashlib.sha256(der).hexdigest().upper()
    return cert, key, ":".join(fingerprint[i : i + 2] for i in range(0, 64, 2))


@dataclass(frozen=True)
class SharedFile:
    id: str
    name: str
    path: Path
    size: int
    sha256: str


class TransferState:
    def __init__(self, directory: Path, max_file: int = MAX_FILE):
        self.directory = directory
        self.received = directory / "received"
        self.received.mkdir(parents=True, exist_ok=True)
        self.max_file = max_file
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        self.code = "".join(secrets.choice(alphabet) for _ in range(12))
        self.sessions: dict[str, tuple[str, float]] = {}
        self.attempts: dict[str, list[float]] = {}
        self.files: dict[str, SharedFile] = {}
        self.lock = threading.RLock()

    def pair(self, ip: str, code: str) -> tuple[int, str | None]:
        with self.lock:
            now = time.monotonic()
            self.attempts = {
                k: [t for t in v if now - t < 60]
                for k, v in self.attempts.items()
                if any(now - t < 60 for t in v)
            }
            failures = self.attempts.setdefault(ip, [])
            if len(failures) >= 5:
                return 429, None
            if not secrets.compare_digest(code.upper().replace("-", "").strip(), self.code):
                failures.append(now)
                return 403, None
            self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
            if len(self.sessions) >= 128:
                return 429, None
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (ip, now + 3600)
            return 200, token

    def authenticated(self, token: str, ip: str) -> bool:
        with self.lock:
            entry = self.sessions.get(token)
            return bool(entry and entry[0] == ip and entry[1] > time.monotonic())

    def publish(self, path: Path) -> SharedFile:
        # Snapshot explicit selections; do not expose a source directory or later edits.
        name = safe_name(path.name)
        size = path.stat().st_size
        if size > self.max_file:
            raise ValueError("文件超过 2 GiB 上限")
        identifier = secrets.token_hex(16)
        target = self.received / (identifier + "_" + name)
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_file:
                        raise ValueError("文件超过大小上限")
                    output.write(chunk)
                    digest.update(chunk)
            item = SharedFile(identifier, name, target, total, digest.hexdigest())
            with self.lock:
                if len(self.files) >= 1000:
                    raise ValueError("共享文件数量已达上限")
                self.files[identifier] = item
            return item
        except BaseException:
            target.unlink(missing_ok=True)
            raise


class LanServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, host: str, port: int, state: TransferState, context: ssl.SSLContext):
        if not is_lan(host):
            raise ValueError("只能监听具体的局域网 IPv4 或回环地址")
        self.state = state
        self.peer_protocol = None
        self.context = context
        self.slots = threading.BoundedSemaphore(8)
        self.connections: set[socket.socket] = set()
        self.connection_lock = threading.Lock()
        super().__init__((host, port), Handler)
        self.authority = f"{host}:{self.server_port}"
        self.origin = f"https://{self.authority}"

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(3)
        if not is_lan(address[0]) or not self.slots.acquire(blocking=False):
            connection.close()
            raise OSError("连接被拒绝")
        try:
            connection = self.context.wrap_socket(connection, server_side=True)
            connection.settimeout(30)
            with self.connection_lock:
                self.connections.add(connection)
            return connection, address
        except BaseException:
            connection.close()
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.connection_lock:
                self.connections.discard(request)
            self.slots.release()

    def stop(self):
        self.shutdown()
        with self.connection_lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.server_close()

    def handle_error(self, request, client_address):
        # Clients may disconnect during transfer; never log secrets or request URLs.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "LinDrop"
    sys_version = ""

    def log_message(self, *_):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        super().end_headers()

    def respond(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def guard(self, mutation=False, authentication=True) -> bool:
        if self.headers.get("Host") != self.server.authority:
            self.respond(403, {"error": "访问地址不匹配"})
            return False
        origin = self.headers.get("Origin")
        if (origin is not None and origin != self.server.origin) or (mutation and not origin):
            self.respond(403, {"error": "禁止跨站请求"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "禁止跨站请求"})
            return False
        if authentication:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie["session"].value if "session" in cookie else ""
            except Exception:
                token = ""
            if not self.server.state.authenticated(token, self.client_address[0]):
                self.respond(401, {"error": "请先配对（连接有效期 1 小时）"})
                return False
        return True

    def body_length(self, limit: int) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块编码")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1 or not values[0].isdigit():
            raise ValueError("缺少有效文件长度")
        length = int(values[0])
        if length > limit:
            raise ValueError("文件超过大小上限")
        return length

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/peer/") and self.server.peer_protocol:
            if self.guard(authentication=False):
                self.server.peer_protocol.get(self, path)
            return
        if not self.guard(authentication=path not in {"/", "/app.js", "/style.css"}):
            return
        if path in {"/", "/app.js", "/style.css"}:
            names = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            name, mime = names[path]
            data = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/files":
            with self.server.state.lock:
                files = [
                    {"id": f.id, "name": f.name, "size": f.size, "sha256": f.sha256}
                    for f in self.server.state.files.values()
                ]
            self.respond(
                200, {"files": files, "version": __version__, "maxFile": self.server.state.max_file}
            )
        elif path.startswith("/download/"):
            with self.server.state.lock:
                item = self.server.state.files.get(path.removeprefix("/download/"))
            if item is None:
                self.respond(404, {"error": "文件不存在或已停止共享"})
                return
            try:
                source = item.path.open("rb")
            except OSError:
                self.respond(404, {"error": "文件已不可用"})
                return
            with source:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(item.size))
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''" + quote(item.name, safe=""),
                )
                self.send_header("X-Content-SHA256", item.sha256)
                self.end_headers()
                shutil.copyfileobj(source, self.wfile, 1024 * 1024)
        else:
            self.respond(404, {"error": "不存在的接口"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/peer/") and self.server.peer_protocol:
            if self.guard(mutation=True, authentication=False):
                self.server.peer_protocol.post(self, path)
            return
        if not self.guard(mutation=True, authentication=path != "/api/pair"):
            return
        if path == "/api/pair":
            try:
                length = self.body_length(1024)
                payload = json.loads(self.rfile.read(length))
                code = payload.get("code", "")
                if not isinstance(code, str) or not code.isascii():
                    raise ValueError("配对码格式不正确")
            except (ValueError, AttributeError):
                self.respond(400, {"error": "配对码格式不正确"})
                return
            status, token = self.server.state.pair(self.client_address[0], code)
            if token:
                self.send_response(200)
                self.send_header(
                    "Set-Cookie",
                    f"session={token}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600",
                )
                self.send_header("Content-Length", "2")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self.respond(
                    status,
                    {"error": "尝试过于频繁，请 60 秒后重试" if status == 429 else "配对码不正确"},
                )
        elif path == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            with self.server.state.lock:
                if "session" in cookie:
                    self.server.state.sessions.pop(cookie["session"].value, None)
            self.respond(200, {})
        elif path == "/api/upload":
            self.upload()
        else:
            self.respond(404, {"error": "不存在的接口"})

    def upload(self):
        state = self.server.state
        try:
            name = safe_name(unquote(self.headers.get("X-Filename", ""), errors="strict"))
            size = self.body_length(state.max_file)
            with state.lock:
                if len(state.files) >= 1000:
                    raise ValueError("共享文件数量已达上限")
            if shutil.disk_usage(state.received).free < size + 128 * 1024**2:
                self.respond(507, {"error": "接收设备磁盘空间不足"})
                return
        except (ValueError, UnicodeError):
            self.respond(400, {"error": "文件名或大小无效（单文件最多 2 GiB）"})
            return
        identifier = secrets.token_hex(16)
        temporary = state.received / (identifier + ".part")
        destination = state.received / (identifier + "_" + name)
        digest = hashlib.sha256()
        remaining = size
        deadline = time.monotonic() + 3600
        try:
            with temporary.open("xb") as output:
                while remaining:
                    if time.monotonic() > deadline:
                        raise TimeoutError("传输超时")
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("传输中断")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            with state.lock:
                if len(state.files) >= 1000:
                    raise ValueError("共享文件数量已达上限")
                temporary.replace(destination)
                item = SharedFile(identifier, name, destination, size, digest.hexdigest())
                state.files[identifier] = item
            self.respond(201, {"id": identifier, "sha256": item.sha256})
        except (OSError, ConnectionError, TimeoutError, ValueError):
            temporary.unlink(missing_ok=True)
            self.respond(400, {"error": "传输中断，未保留不完整文件，请重试"})


def create_server(host: str, port: int, directory: Path) -> tuple[LanServer, str]:
    if not is_lan(host):
        raise ValueError("只支持局域网 IPv4 地址")
    cert, key, fingerprint = prepare_certificate(directory, host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    state = TransferState(directory)
    return LanServer(host, port, state, context), fingerprint
