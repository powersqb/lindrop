"""Tk lifecycle, compact layout and native drop payload handling.

Synthetic peers/messages live only on loopback. Human comparison is simulated
inside the test harness, never bypassed by the normal UI.
"""

import time
from types import SimpleNamespace

import pytest

from lan_drop.desktop import Desktop
from lan_drop.peer import PeerNode


def pump(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.window.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Tk condition timed out")


@pytest.fixture
def desktop(tmp_path):
    app = Desktop(SimpleNamespace(host="127.0.0.1", port=0, data_dir=tmp_path / "ui", name="UI A"))
    app.window.withdraw()
    try:
        pump(app, lambda: app.node is not None)
        yield app
    finally:
        app.close()


def test_native_drop_and_compact_layout(desktop, tmp_path):
    app = desktop
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", "UI B", discover=False)
    try:
        peer = b.as_peer()
        app.manual[peer.id] = peer
        app.update_peers()
        app.devices.selection_set(peer.id)
        app.select_peer()
        source = tmp_path / "中文 带空格.txt"
        source.write_text("测试拖拽", encoding="utf-8")
        # splitlist exercises the exact Tcl path transport used by TkDnD.
        payload = app.window.tk.call("list", str(source))
        assert app.drop_files(SimpleNamespace(data=payload)) == "copy"
        pump(app, lambda: bool(b.protocol.offers))
        incoming = next(iter(b.protocol.offers.values()))
        assert incoming.files[0]["name"] == source.name
        assert incoming.status == "pending"
        assert not list(b.received.iterdir())
        app.cancel_send()
        pump(app, lambda: not app.sending)
        assert (
            app.drop_files(SimpleNamespace(data=app.window.tk.call("list", str(tmp_path))))
            == "none"
        )
        assert app.window.tk.call("package", "present", "tkdnd")
        app.window.deiconify()
        app.window.geometry("640x520")
        # Concurrent incoming/outgoing authorization cards must keep the composer visible.
        app.send_card.pack(fill="x", before=app.composer)
        app.receive_card.pack(fill="x", before=app.composer)
        app.receive_text.set("测试设备发来两个文件，请核对验证码再接收")
        app.window.update()
        for widget in (app.message_button, app.composer, app.file_list, app.chat_button):
            assert widget.winfo_ismapped()
            assert widget.winfo_y() >= 0
            assert (
                widget.winfo_rooty() + widget.winfo_height()
                <= app.window.winfo_rooty() + app.window.winfo_height()
            )
        app.file_peers[peer.id] = peer
        app.devices.selection_remove(peer.id)
        app.add_history("接收", "文件.txt", "已校验", peer.id)
        assert app.conversations[peer.id][-1][1] == "文件.txt · 已校验"
    finally:
        b.close()


def test_chat_ui_delivery_and_composer_preservation(desktop, tmp_path):
    app = desktop
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", "UI B", discover=False)
    try:
        peer = b.as_peer()
        app.manual[peer.id] = peer
        app.update_peers()
        app.devices.selection_set(peer.id)
        app.select_peer()
        app.chat_toggle()
        pump(app, lambda: bool(b.chat.channels))
        session = app.sessions[peer.id]
        b.chat.decide(session["id"], True)
        session["confirmed"].set()  # test-only stand-in for trusted human comparison
        pump(app, lambda: session["ready"])
        app.composer.insert("1.0", "你好，小窗口")
        app.send_message()
        app.composer.insert("end", "继续编辑")
        pump(app, lambda: not session.get("busy"))
        assert app.composer.get("1.0", "end-1c").endswith("继续编辑")
        channel = b.chat.channels[session["id"]]
        assert channel.messages[0]["text"] == "你好，小窗口"
        b.chat.append(channel.id, "收到", "b" * 32, "receiver")
        pump(app, lambda: "收到" in app.transcript.get("1.0", "end"))
        app.end_chat(peer.id)
        assert peer.id not in app.sessions
    finally:
        b.close()
