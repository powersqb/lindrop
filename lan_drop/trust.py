"""Remember only human-verified device identities; discovery is never trusted."""

import json
import threading
from pathlib import Path

from .discovery import HEX32, HEX64, device_name


class TrustStore:
    def __init__(self, directory):
        self.path = Path(directory) / "trusted-devices.json"
        self.lock = threading.RLock()
        self.peers = {}
        if self.path.exists():
            if self.path.stat().st_size > 65536:
                raise ValueError("设备信任记录过大，请恢复备份")
            value = json.loads(self.path.read_text("utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError("设备信任记录无效，请恢复备份")
            peers = value.get("peers")
            if not isinstance(peers, dict) or len(peers) > 128:
                raise ValueError("设备信任记录无效")
            for identifier, item in peers.items():
                if (
                    not HEX32.fullmatch(identifier)
                    or not isinstance(item, dict)
                    or not isinstance(item.get("fingerprint"), str)
                    or not HEX64.fullmatch(item["fingerprint"])
                ):
                    raise ValueError("设备信任记录无效")
                self.peers[identifier] = {
                    "name": device_name(item.get("name")),
                    "fingerprint": item["fingerprint"],
                }

    def check(self, peer):
        with self.lock:
            item = self.peers.get(peer.id)
            if item and item["fingerprint"] != peer.fingerprint:
                raise ValueError("设备证书已变化，已阻止连接；请向对方核实，再在设备身份中忘记设备")
            return item is not None

    def save(self, peers):
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({"version": 1, "peers": peers}, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(self.path)
            self.peers = peers
        finally:
            temporary.unlink(missing_ok=True)

    def remember(self, peer):
        with self.lock:
            self.check(peer)
            if not HEX32.fullmatch(peer.id) or not HEX64.fullmatch(peer.fingerprint):
                raise ValueError("设备身份无效")
            if peer.id not in self.peers and len(self.peers) >= 128:
                raise ValueError("已记住128台设备，请先忘记不用的设备")
            self.save({**self.peers, peer.id: {"name": peer.name, "fingerprint": peer.fingerprint}})

    def forget(self, identifier):
        with self.lock:
            self.save({k: v for k, v in self.peers.items() if k != identifier})
