"""Bounded pinned-TLS presence replies when a peer cannot receive multicast.

Presence is a discovery hint, never device trust or permission to send files.
Only endpoints advertised by app beacons are contacted; no address scanning.
"""

import json
import queue
import threading
import time

from .discovery import parse_beacon


class Presence:
    def __init__(self, node):
        self.node = node
        self.lock = threading.Lock()
        self.work = queue.Queue(maxsize=16)
        self.pending = set()
        self.attempts = {}
        self.incoming = {}
        self.online = {}
        for _ in range(2):
            threading.Thread(target=self.loop, daemon=True, name="lindrop-presence").start()

    def submit(self, peer):
        if self.node.closed.is_set() or peer.id == self.node.id or not peer.presence:
            return
        now = time.monotonic()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v < 60}
            if (
                peer.id in self.pending
                or now - self.attempts.get(peer.id, -60) < 4
                or (peer.id not in self.attempts and len(self.attempts) >= 128)
            ):
                return
            try:
                self.work.put_nowait(peer)
            except queue.Full:
                return
            self.pending.add(peer.id)
            self.attempts[peer.id] = now

    def loop(self):
        from .peer import peer_request

        while not self.node.closed.is_set():
            try:
                peer = self.work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self.node.closed.is_set():
                    continue
                self.node.trust.check(peer)
                hello = peer_request(peer, "GET", "/peer/hello", source=self.node.host, timeout=3)
                if (
                    not isinstance(hello, dict)
                    or hello.get("id") != peer.id
                    or hello.get("presence") is not True
                ):
                    continue
                if not self.node.closed.is_set():
                    peer_request(
                        peer,
                        "POST",
                        "/peer/presence",
                        {"app": "lindrop", "protocol": 2, **self.node.identity()},
                        source=self.node.host,
                        timeout=3,
                    )
            except (ValueError, OSError):
                pass  # Unknown or offline endpoints stay untrusted; never learn a new pin.
            finally:
                with self.lock:
                    self.pending.discard(peer.id)
                self.work.task_done()

    def accept(self, handler, payload):
        from .peer import peer_request

        ip, now = handler.client_address[0], time.monotonic()
        with self.lock:
            self.incoming = {k: v for k, v in self.incoming.items() if now - v < 60}
            if now - self.incoming.get(ip, -60) < 2 or (
                ip not in self.incoming and len(self.incoming) >= 128
            ):
                raise ValueError("设备发现请求过于频繁")
            self.incoming[ip] = now
        # Ignore payload host hints here: reverse validation uses the actual TCP source.
        peer = parse_beacon(json.dumps({**payload, "host": ip}).encode(), ip)
        self.node.trust.check(peer)
        hello = peer_request(peer, "GET", "/peer/hello", source=self.node.host, timeout=3)
        if not isinstance(hello, dict) or hello.get("id") != peer.id or hello.get("protocol") != 2:
            raise ValueError("设备发现身份不一致")
        with self.lock:
            if peer.id not in self.online and len(self.online) >= 128:
                raise ValueError("设备发现数量已达上限")
            if peer.id != self.node.id:
                self.online[peer.id] = peer
        handler.respond(200, {"id": self.node.id, "discovered": True})

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            self.online = {k: p for k, p in self.online.items() if now - p.seen < 8}
            return list(self.online.values())
