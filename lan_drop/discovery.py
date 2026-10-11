"""App-only multicast presence. Never scan addresses or send cloud traffic."""

import json
import re
import socket
import threading
import time
from dataclasses import dataclass

from .server import is_lan

GROUP = "239.255.42.99"
PORT = 45876
EXPIRE = 8
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def device_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("设备名格式不正确")
    value = value.strip()
    if not value or len(value) > 40 or any(ord(c) < 32 for c in value):
        raise ValueError("设备名需要 1 至 40 个可见字符")
    return value


@dataclass(frozen=True)
class Peer:
    id: str
    name: str
    host: str
    port: int
    fingerprint: str
    seen: float


def parse_beacon(data: bytes, host: str) -> Peer:
    if len(data) > 1500 or not is_lan(host):
        raise ValueError("无效发现数据")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("app") != "lindrop" or value.get("protocol") != 2:
        raise ValueError("非邻传设备")
    identifier, fingerprint = value.get("id", ""), value.get("fingerprint", "")
    port = value.get("port")
    if (
        not isinstance(identifier, str)
        or not HEX32.fullmatch(identifier)
        or not isinstance(fingerprint, str)
        or not HEX64.fullmatch(fingerprint)
        or type(port) is not int
        or not 1024 <= port <= 65535
    ):
        raise ValueError("无效设备信息")
    return Peer(
        identifier, device_name(value.get("name")), host, port, fingerprint, time.monotonic()
    )


class Discovery:
    def __init__(self, host: str, identity, event):
        self.host, self.identity, self.event = host, identity, event
        self.peers: dict[str, Peer] = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(("", PORT))
            self.socket.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(GROUP) + socket.inet_aton(host),
            )
            self.socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(host)
            )
            self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self.socket.settimeout(0.5)
        except BaseException:
            self.socket.close()
            raise
        threading.Thread(target=self.loop, daemon=True).start()

    def announce(self):
        beacon = {"app": "lindrop", "protocol": 2, **self.identity()}
        self.socket.sendto(json.dumps(beacon, ensure_ascii=False).encode(), (GROUP, PORT))

    def remember(self, peer):
        if peer.id == self.identity()["id"]:
            return
        with self.lock:
            if peer.id not in self.peers and len(self.peers) >= 128:
                return
            self.peers[peer.id] = peer

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            self.peers = {key: peer for key, peer in self.peers.items() if now - peer.seen < EXPIRE}
            return sorted(self.peers.values(), key=lambda peer: (peer.name, peer.host))

    def loop(self):
        next_beacon = 0
        while not self.closed.is_set():
            if time.monotonic() >= next_beacon:
                try:
                    self.announce()
                except OSError:
                    if not self.closed.is_set():
                        self.event("notice", text="设备发现发送失败，请检查当前网络")
                next_beacon = time.monotonic() + 2
            try:
                packet, source = self.socket.recvfrom(1501)
                self.remember(parse_beacon(packet, source[0]))
            except (ValueError, UnicodeError, socket.timeout):
                pass
            except OSError:
                if self.closed.is_set():
                    return

    def close(self):
        self.closed.set()
        self.socket.close()
