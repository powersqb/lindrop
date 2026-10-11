"""Tk lifecycle, compact layout and native drop payload handling.

Synthetic peers/messages live only on loopback. Human comparison is simulated
inside the test harness, never bypassed by the normal UI.
"""

import time
from types import SimpleNamespace

from lan_drop.peer import PeerNode


def test_file_send_uses_verified_chat_endpoint_when_udp_source_is_rewritten(desktop, tmp_path):
    from lan_drop.discovery import Peer
    from lan_drop.peer import peer_request

    app = desktop
    phone = PeerNode("127.0.0.1", 0, tmp_path / "phone", "Phone", discover=False)
    try:
        peer = phone.as_peer()
        app.node.trust.remember(peer)
        phone.trust.remember(app.node.as_peer())
        # Reproduce the phone initiating chat while its UDP beacon is relayed
        # with the PC's address/port. The old file path connects to the PC and
        # fails the phone certificate pin, despite chat working.
        beacon = Peer(peer.id, peer.name, app.node.host, app.node.port, peer.fingerprint, peer.seen)
        app.node.discovery = SimpleNamespace(snapshot=lambda: [beacon], close=lambda: None)
        offer = phone.begin(app.node.as_peer(), [], chat=True, automatic=True)
        peer_request(
            app.node.as_peer(),
            "POST",
            f"/peer/chat/{offer['id']}/activate",
            {},
            offer["secret"],
            phone.host,
        )
        pump(app, lambda: app.sessions.get(peer.id, {}).get("ready"))
        app.update_peers()
        assert app.peers[peer.id].port == phone.port
        app.devices.selection_set(peer.id)
        source = tmp_path / "original.txt"
        source.write_bytes(b"file transfer after incoming chat")
        app.send(paths=[source])
        pump(app, lambda: bool(phone.protocol.offers))
        incoming = next(iter(phone.protocol.offers.values()))
        phone.protocol.decide(incoming.id, True)
        pump(app, lambda: not app.sending)
        assert (phone.received / source.name).read_bytes() == source.read_bytes()
        assert app.file_peers[peer.id].fingerprint == peer.fingerprint
        # A stopped or no-longer-trusted session must not refresh the route.
        app.sessions[peer.id]["cancel"].set()
        app.manual.clear()
        app.update_peers()
        assert app.peers[peer.id].port == app.node.port
    finally:
        phone.close()


def pump(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.window.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Tk condition timed out")


def test_native_drop_and_compact_layout(desktop, tmp_path):
    app = desktop
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", "UI B", discover=False)
    try:
        peer = b.as_peer()
        app.manual[peer.id] = peer
        app.update_peers()
        app.devices.selection_set(peer.id)
        app.select_peer()
        app.node.trust.remember(peer)  # previously human-verified identity
        b.trust.remember(app.node.as_peer())
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
        app.receive_card.pack(fill="x", before=app.file_list)
        app.receive_text.set("测试设备发来两个文件，请核对验证码再接收")
        app.window.update()
        for widget in (app.message_button, app.composer, app.file_list, app.send_button):
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
        app.composer.insert("1.0", "你好，小窗口")
        app.send_message()
        pump(app, lambda: bool(b.chat.channels) and bool(app.sessions[peer.id].get("id")))
        session = app.sessions[peer.id]
        b.chat.decide(session["id"], True)
        session["confirmed"].set()  # test-only stand-in for trusted human comparison
        app.composer.insert("end", "继续编辑")
        pump(app, lambda: session["ready"])
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


def test_manual_updates_do_not_connect_on_open(desktop, monkeypatch):
    from lan_drop.updates import Release

    calls = []
    release = Release(
        "0.5.0",
        "https://github.com/powersqb/lindrop/releases/tag/v0.5.0",
        "https://github.com/powersqb/lindrop/releases/download/v0.5.0/lindrop-v0.5.0-windows-x64.zip",
        "lindrop-v0.5.0-windows-x64.zip",
        "a" * 64,
        100,
        "新增功能说明",
    )

    def check(_):
        calls.append("check")
        return release, True

    monkeypatch.setattr("lan_drop.update_ui.check_latest", check)
    desktop.window.deiconify()
    desktop.show_updates()
    desktop.window.update()
    assert calls == []  # Opening settings is not permission to contact GitHub.
    desktop.check_updates()
    pump(desktop, lambda: not desktop.update_busy)
    assert calls == ["check"]
    assert desktop.update_release == release
    assert "0.5.0" in desktop.update_status.get()
    desktop.update_window.geometry("420x320")
    desktop.window.update()
    for widget in (
        desktop.check_button,
        desktop.download_button,
        desktop.update_open_button,
        desktop.install_button,
    ):
        assert widget.winfo_ismapped()
        assert widget.winfo_rooty() + widget.winfo_height() <= (
            desktop.update_window.winfo_rooty() + desktop.update_window.winfo_height()
        )
    desktop.close_updates()
    assert desktop.update_cancel.is_set()


def test_receive_collision_requires_local_choice(desktop, monkeypatch):
    from types import SimpleNamespace

    offer = SimpleNamespace(files=[{"name": "原名.txt"}, {"name": "原名.txt"}])
    (desktop.node.received / "原名.txt").write_text("existing")
    monkeypatch.setattr("lan_drop.desktop.messagebox.askyesnocancel", lambda *a, **kw: True)
    monkeypatch.setattr("lan_drop.desktop.simpledialog.askstring", lambda *a, **kw: "自己选的.txt")
    assert desktop.receive_names(offer) == [("原名.txt", True), ("自己选的.txt", False)]
    monkeypatch.setattr("lan_drop.desktop.messagebox.askyesnocancel", lambda *a, **kw: None)
    assert desktop.receive_names(offer) is None
    assert (desktop.node.received / "原名.txt").read_text() == "existing"


def test_install_blocks_active_transfer_and_source_runtime(desktop, monkeypatch, tmp_path):
    desktop.show_updates()
    desktop.update_path = tmp_path / "test.zip"
    desktop.update_release = object()
    notices = []
    monkeypatch.setattr(
        "lan_drop.update_ui.messagebox.showinfo", lambda *a, **kw: notices.append(a)
    )
    desktop.sending = True
    desktop.install_update()
    assert len(notices) == 1 and not desktop.update_busy
    desktop.sending = False
    desktop.install_update()
    assert "源码运行" in desktop.update_status.get()
    assert not desktop.update_busy


def test_close_during_install_preparation_cancels_helper(desktop, tmp_path):
    desktop.show_updates()
    desktop.update_manifest = tmp_path / "job.json"
    desktop.close()
    assert (tmp_path / "cancel").exists()


def test_install_ready_closes_app_without_cancelling_task(desktop, tmp_path, monkeypatch):
    desktop.show_updates()
    desktop.update_manifest = tmp_path / "job.json"
    calls = []
    monkeypatch.setattr(desktop, "close", lambda: calls.append(True))
    desktop.handle_update({"kind": "update_install_ready", "manifest": desktop.update_manifest})
    desktop.cancel_install()
    assert calls == [True] and not (tmp_path / "cancel").exists()
