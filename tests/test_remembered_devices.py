"""Real TLS reconnects and simultaneous Tk sends with one-time verification."""

import dataclasses
import time
from types import SimpleNamespace

import pytest

from lan_drop.desktop import Desktop
from lan_drop.peer import PeerNode


def pump(apps, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for app in apps:
            app.window.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Tk integration timed out")


def select(app, other):
    peer = other.node.as_peer()
    app.manual[peer.id] = peer
    app.update_peers()
    app.devices.selection_set(peer.id)
    app.select_peer()


def accept_first_identity(app):
    def buttons(widget):
        for child in widget.winfo_children():
            if child.winfo_class() == "TButton" and child.cget("text") == "一致，记住设备":
                yield child
            yield from buttons(child)

    dialog = next(iter(app.chat_dialogs.values()))
    next(buttons(dialog)).invoke()


def test_remembered_identity_survives_restart_and_blocks_changed_pin(tmp_path):
    a = PeerNode("127.0.0.1", 0, tmp_path / "a", discover=False)
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", discover=False)
    try:
        original = b.as_peer()
        a.trust.remember(original)  # simulated previous human comparison
        b.trust.remember(a.as_peer())
        a.close()
        b.close()
        a = PeerNode("127.0.0.1", 0, tmp_path / "a", discover=False)
        b = PeerNode("127.0.0.1", 0, tmp_path / "b", discover=False)
        assert b.id == original.id and b.fingerprint == original.fingerprint
        assert a.trust.check(b.as_peer())
        offer = a.begin(b.as_peer(), [], chat=True, automatic=True)
        assert b.chat.channels[offer["id"]].status == "accepted"
        with pytest.raises(ValueError, match="证书已变化"):
            a.begin(dataclasses.replace(b.as_peer(), fingerprint="f" * 64), [], chat=True)
        a.trust.forget(b.id)
        assert not a.trust.check(b.as_peer())
    finally:
        a.close()
        b.close()


def test_simultaneous_first_messages_then_direct_reconnect(desktop, tmp_path):
    a = desktop
    args = SimpleNamespace(host="127.0.0.1", port=0, data_dir=tmp_path / "b-ui", name="直聊 B")
    b = Desktop(args)
    b.window.withdraw()
    try:
        pump([a, b], lambda: b.node is not None)
        select(a, b)
        select(b, a)
        a.composer.insert("1.0", "A 的首条消息")
        b.composer.insert("1.0", "B 的首条消息")
        a.send_message()
        b.send_message()
        pump([a, b], lambda: bool(a.chat_dialogs) and bool(b.chat_dialogs))
        assert a.sessions[b.node.id]["code"] == b.sessions[a.node.id]["code"]
        assert not a.conversations.get(b.node.id)  # no text delivered before comparison
        accept_first_identity(a)
        accept_first_identity(b)
        pump(
            [a, b],
            lambda: (
                "B 的首条消息" in a.transcript.get("1.0", "end")
                and "A 的首条消息" in b.transcript.get("1.0", "end")
            ),
        )
        assert a.node.trust.check(b.node.as_peer()) and b.node.trust.check(a.node.as_peer())
        assert not a.chat_dialogs and not b.chat_dialogs
        old_id, old_pin = b.node.id, b.node.fingerprint
        b.close()
        b = Desktop(args)
        b.window.withdraw()
        pump([a, b], lambda: b.node is not None)
        assert (b.node.id, b.node.fingerprint) == (old_id, old_pin)
        select(a, b)
        select(b, a)
        b.composer.insert("1.0", "重开后直接发送")
        b.send_message()
        pump([a, b], lambda: "重开后直接发送" in a.transcript.get("1.0", "end"))
        assert not a.chat_dialogs and not b.chat_dialogs
        assert not hasattr(a, "chat_button")
    finally:
        b.close()


def test_file_sender_only_receiver_decides_and_input_above_files(desktop, tmp_path):
    a = desktop
    b = Desktop(
        SimpleNamespace(host="127.0.0.1", port=0, data_dir=tmp_path / "b-files", name="文件 B")
    )
    b.window.withdraw()
    try:
        pump([a, b], lambda: b.node is not None)
        select(a, b)
        source = tmp_path / "只确认接收.txt"
        source.write_text("传输成功", encoding="utf-8")
        a.paths = [str(source)]
        a.send()
        pump([a, b], lambda: bool(a.chat_dialogs) and bool(b.chat_dialogs))
        assert not b.node.protocol.offers
        accept_first_identity(a)
        accept_first_identity(b)
        pump([a, b], lambda: bool(b.incoming))
        assert not list(b.node.received.iterdir())
        assert b.accept_button.cget("text") == "接收"
        assert not hasattr(a, "verify_button")
        b.decide(True)
        pump([a, b], lambda: not a.sending and not b.incoming)
        assert next(b.node.received.iterdir()).read_text("utf-8") == "传输成功"
        a.send()
        pump([a, b], lambda: bool(b.incoming))
        b.decide(False)
        pump([a, b], lambda: not a.sending and not b.incoming)
        assert len(list(b.node.received.iterdir())) == 1
        a.window.deiconify()
        a.window.geometry("640x520")
        a.window.update()
        assert a.transcript.winfo_rooty() + a.transcript.winfo_height() <= a.composer.winfo_rooty()
        assert a.composer.winfo_rooty() + a.composer.winfo_height() < a.choose_button.winfo_rooty()
    finally:
        b.close()
