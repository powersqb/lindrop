"""Packaged diagnostics with two isolated local desktop peers."""

import dataclasses
import hashlib
import json
import ssl
import threading
import time
import traceback
from pathlib import Path

from tkinterdnd2 import DND_FILES, TkinterDnD

from . import __version__
from .peer import PeerNode, describe_files, peer_request
from .server import local_addresses


def run(directory: Path, host=None):
    directory.mkdir(parents=True, exist_ok=True)
    report = {"ok": False, "version": __version__, "checks": []}
    nodes = []
    try:
        window = TkinterDnD.Tk()
        window.drop_target_register(DND_FILES)
        window.withdraw()
        window.update()
        window.destroy()
        report["checks"].append("bundled Tk and native TkDnD startup")
        addresses = local_addresses() if host is None else ([] if host == "127.0.0.1" else [host])
        host = addresses[0] if addresses else "127.0.0.1"
        a = PeerNode(host, 0, directory / "a", "诊断设备 A", discover=bool(addresses))
        nodes.append(a)
        b = PeerNode(host, 0, directory / "b", "诊断设备 B", discover=bool(addresses))
        nodes.append(b)
        if addresses:
            assert a.discovery and b.discovery
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if b.id in {p.id for p in a.discovery.snapshot()} and a.id in {
                    p.id for p in b.discovery.snapshot()
                }:
                    break
                time.sleep(0.1)
            assert b.id in {p.id for p in a.discovery.snapshot()}
            assert a.id in {p.id for p in b.discovery.snapshot()}
            report["checks"].append("real LAN multicast discovery between two local app instances")
        else:
            report["discovery"] = "not tested: no LAN adapter"
        wrong = dataclasses.replace(b.as_peer(), fingerprint="0" * 64)
        try:
            peer_request(wrong, "GET", "/peer/hello", source=a.host)
        except ssl.SSLError:
            report["checks"].append("certificate pin mismatch blocked before control request")
        else:
            raise AssertionError("Incorrect certificate accepted")
        payload = b"LinDrop desktop transfer integrity\n" * 32768
        source = directory / "diagnostic.txt"
        source.write_bytes(payload)
        for sender, receiver in [(a, b), (b, a)]:
            files = describe_files([source], threading.Event(), lambda *_: None)
            offer = sender.begin(receiver.as_peer(), files)
            assert not list(receiver.received.iterdir())
            assert offer["code"] == receiver.protocol.offers[offer["id"]].code
            # Test-only first-use identity comparison and receiver file acceptance.
            sender.trust.remember(receiver.as_peer())
            receiver.trust.remember(sender.as_peer())
            receiver.protocol.decide(offer["id"], True)
            token = sender.wait_accepted(receiver.as_peer(), offer, threading.Event())
            sender.send_file(
                receiver.as_peer(),
                offer,
                token,
                0,
                source,
                files[0],
                threading.Event(),
                lambda *_: None,
            )
            saved = list(receiver.received.iterdir())
            assert len(saved) == 1 and saved[0].read_bytes() == payload
            assert receiver.protocol.offers[offer["id"]].status == "complete"
        report["checks"].append(
            "one-time identity comparison, receiver acceptance and bidirectional HTTPS transfer"
        )
        import secrets

        offer = a.begin(b.as_peer(), [], chat=True, automatic=True)
        assert b.chat.channels[offer["id"]].code == offer["code"]
        assert b.chat.channels[offer["id"]].status == "accepted"
        peer_request(
            b.as_peer(), "POST", f"/peer/chat/{offer['id']}/activate", {}, offer["secret"], a.host
        )
        peer_request(
            b.as_peer(),
            "POST",
            f"/peer/chat/{offer['id']}/send",
            {"message_id": secrets.token_hex(16), "text": "中文聊天 hello"},
            offer["secret"],
            a.host,
        )
        b.chat.append(offer["id"], "收到", secrets.token_hex(16), "receiver")
        reply = peer_request(
            b.as_peer(), "GET", f"/peer/chat/{offer['id']}/0", secret=offer["secret"], source=a.host
        )
        assert [m["text"] for m in reply["messages"]] == ["中文聊天 hello", "收到"]
        report["checks"].append(
            "remembered devices connect automatically for bidirectional encrypted Unicode chat"
        )
        report["sha256"] = hashlib.sha256(payload).hexdigest()
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        for node in nodes:
            node.close()
        (directory / "self-test.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
