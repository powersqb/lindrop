"""Exercise the actual Android Java protocol against Windows Python protocol.

This verifies the transport/core, not Android Keystore/UI or HarmonyOS containers.
Requires a compiled Android debug project and org.json JVM test dependency.
"""

import argparse
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lan_drop.peer import PeerNode, describe_files, peer_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--json-jar", type=Path, required=True)
    parser.add_argument("--classes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    desktop = PeerNode("127.0.0.1", 0, args.output / "desktop", "Windows Python", discover=False)
    java = None
    records, java_events = [], queue.Queue()
    errors = []
    stop = threading.Event()

    def desktop_events():
        while not stop.is_set():
            try:
                event = desktop.events.get(timeout=0.2)
                if event["kind"] == "chat_request":
                    if event["channel"].status == "pending":
                        desktop.chat.decide(event["channel"].id, True)
                elif event["kind"] == "incoming":
                    desktop.protocol.decide(event["id"], True)
            except queue.Empty:
                pass
            except Exception as error:
                errors.append(str(error))

    threading.Thread(target=desktop_events, daemon=True).start()

    def java_until(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = java_events.get(timeout=0.2)
                if predicate(record):
                    return record
            except queue.Empty:
                if java.poll() is not None:
                    raise RuntimeError("Java harness exited")
        raise TimeoutError("Java event timeout")

    def command(**payload):
        java.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        java.stdin.flush()

    try:
        openssl = (
            Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe") if os.name == "nt" else "openssl"
        )
        key, cert, p12 = (args.output / name for name in ("test.key", "test.crt", "test.p12"))
        subprocess.run(
            [
                str(openssl),
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=Android JVM test",
                "-days",
                "1",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                str(openssl),
                "pkcs12",
                "-export",
                "-inkey",
                str(key),
                "-in",
                str(cert),
                "-out",
                str(p12),
                "-passout",
                "pass:interop-only",
            ],
            check=True,
            capture_output=True,
        )
        classes = args.output / "harness-classes"
        classes.mkdir(exist_ok=True)
        cp = os.pathsep.join(
            map(str, [args.classes.resolve(), args.json_jar.resolve(), classes.resolve()])
        )
        source = Path(__file__).resolve().parents[1] / "android/interop/JvmHarness.java"
        subprocess.run(
            [
                str(args.java_home / "bin/javac"),
                "-encoding",
                "UTF-8",
                "-cp",
                cp,
                "-d",
                str(classes),
                str(source),
            ],
            check=True,
        )
        java = subprocess.Popen(
            [
                str(args.java_home / "bin/java"),
                "-Dfile.encoding=UTF-8",
                "-cp",
                cp,
                "cn.lindrop.app.JvmHarness",
                str(p12.resolve()),
                str((args.output / "android-received").resolve()),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=(args.output / "java-stderr.log").open("w"),
            text=True,
            encoding="utf-8",
        )

        def read_java():
            for line in java.stdout:
                record = json.loads(line)
                records.append(record)
                java_events.put(record)

        threading.Thread(target=read_java, daemon=True).start()
        ready = java_until(lambda e: e["event"] == "ready")
        peer = desktop.manual_peer("127.0.0.1", ready["port"])
        desktop.presence.submit(__import__("dataclasses").replace(peer, presence=True))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            command(action="presence-status", peer=desktop.identity())
            presence = java_until(lambda e: e["event"] == "presence-status")
            if presence["discovered"]:
                break
            time.sleep(0.2)
        assert presence["discovered"] and not presence["trusted"], "Presence must not auto-trust"
        channel = desktop.begin(peer, [], chat=True, automatic=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = peer_request(
                peer,
                "GET",
                f"/peer/chat/{channel['id']}/0",
                secret=channel["secret"],
                source=desktop.host,
            )
            if result["status"] == "accepted":
                break
            time.sleep(0.1)
        assert result["status"] == "accepted"
        confirm = java_until(lambda e: e["event"] == "confirm")
        assert channel["code"] in confirm["text"], "SAS mismatch"
        desktop.trust.remember(peer)
        peer_request(
            peer,
            "POST",
            f"/peer/chat/{channel['id']}/activate",
            {},
            channel["secret"],
            desktop.host,
        )
        peer_request(
            peer,
            "POST",
            f"/peer/chat/{channel['id']}/send",
            {"text": "电脑 → 手机 🎈", "message_id": secrets.token_hex(16)},
            channel["secret"],
            desktop.host,
        )
        java_until(lambda e: e.get("text") == "电脑 → 手机 🎈")
        command(action="chat", peer=desktop.identity(), text="手机 → 电脑 ✅")
        java_until(lambda e: e.get("text") == "手机 → 电脑 ✅" and e.get("mine"))
        result = peer_request(
            peer,
            "GET",
            f"/peer/chat/{channel['id']}/0",
            secret=channel["secret"],
            source=desktop.host,
        )
        assert any(
            m["text"] == "手机 → 电脑 ✅" and m["author"] == "receiver" for m in result["messages"]
        )
        source_file = args.output / "原名 测试🎈.bin"
        content = bytes(range(256)) * 4096 + b"\0\r\nunaltered"
        source_file.write_bytes(content)
        metadata = describe_files([source_file], threading.Event(), lambda *a: None)
        offer = desktop.begin(peer, metadata)
        token = desktop.wait_accepted(peer, offer, threading.Event())
        desktop.send_file(
            peer, offer, token, 0, source_file, metadata[0], threading.Event(), lambda *a: None
        )
        target = args.output / "android-received" / source_file.name
        assert target.read_bytes() == content
        # Corrupt uploads must preserve an existing file, even after overwrite approval.
        wrong = [dict(metadata[0], sha256="0" * 64)]
        invalid = desktop.begin(peer, wrong)
        invalid_token = desktop.wait_accepted(peer, invalid, threading.Event())
        try:
            desktop.send_file(
                peer,
                invalid,
                invalid_token,
                0,
                source_file,
                wrong[0],
                threading.Event(),
                lambda *a: None,
            )
            raise AssertionError("Corrupt upload accepted")
        except ValueError as error:
            assert "SHA-256" in str(error)
        assert target.read_bytes() == content
        assert not list(target.parent.glob("*.part"))
        cancelled = desktop.begin(peer, metadata)
        cancelled_token = desktop.wait_accepted(peer, cancelled, threading.Event())
        desktop.cancel(peer, cancelled)
        try:
            desktop.send_file(
                peer,
                cancelled,
                cancelled_token,
                0,
                source_file,
                metadata[0],
                threading.Event(),
                lambda *a: None,
            )
            raise AssertionError("Cancelled upload accepted")
        except (ValueError, OSError):
            pass
        assert target.read_bytes() == content
        copy = args.output / "outgoing-cache.bin"
        copy.write_bytes(content)
        command(
            action="files", peer=desktop.identity(), path=str(copy.resolve()), name=source_file.name
        )
        java_until(lambda e: e.get("text") == "文件发送完成")
        assert (desktop.received / source_file.name).read_bytes() == content
        command(action="wrong-pin", peer=desktop.identity())
        java_until(lambda e: e["event"] == "wrong-pin-blocked")
        # Start a new channel in the opposite direction, with remembered devices.
        peer_request(
            peer, "POST", f"/peer/chat/{channel['id']}/close", {}, channel["secret"], desktop.host
        )
        command(action="chat", peer=desktop.identity(), text="安卓主动连接")
        java_until(lambda e: e.get("text") == "安卓主动连接" and e.get("mine"))
        active = next(c for c in desktop.chat.channels.values() if c.status == "active")
        desktop.chat.append(active.id, "电脑轮询回复", secrets.token_hex(16), "receiver")
        java_until(lambda e: e.get("text") == "电脑轮询回复" and not e.get("mine"))
        assert not errors, errors
        report = {
            "date": "2026-10-11",
            "ok": True,
            "scope": "Android Java core on JVM ↔ Windows Python core, loopback TLS",
            "checks": [
                "matching first-use SAS",
                "Unicode chat both directions",
                "bidirectional exact filename and bytes",
                "wrong certificate pin blocked",
                "corrupt overwrite preserves existing file",
                "cancel blocks subsequent upload",
                "remembered Android initiated chat and polling reply",
            ],
            "android_runtime_verified": False,
            "mate80_verified": False,
        }
        (args.output / "verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True))
    finally:
        stop.set()
        desktop.close()
        if java is not None:
            if java.poll() is None:
                command(action="close")
                java.wait(timeout=10)
        (args.output / "events.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
