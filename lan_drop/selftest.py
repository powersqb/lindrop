"""Packaged diagnostics with two isolated local desktop peers."""

import dataclasses
import hashlib
import json
import ssl
import threading
import time
import tkinter
import traceback
from pathlib import Path

from . import __version__
from .peer import PeerNode, describe_files, peer_request
from .server import local_addresses


def run(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    report = {"ok": False, "version": __version__, "checks": []}
    nodes = []
    try:
        window = tkinter.Tk()
        window.withdraw()
        window.update()
        window.destroy()
        report["checks"].append("bundled Tk startup")
        addresses = local_addresses()
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
            # Test-only simulated user acceptance; the normal UI requires both users.
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
            "matching verification codes, explicit acceptance and bidirectional HTTPS transfer"
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
