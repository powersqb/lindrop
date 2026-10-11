"""Chat UI extension: all network I/O runs off the Tk main thread."""

import secrets
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

from .chat import message_text
from .peer import peer_request


class ChatUI:
    def selected_peer(self):
        selected = self.devices.selection()
        return self.peers.get(selected[0]) if selected else None

    def log(self, peer, author, text):
        if peer.id not in self.conversations and len(self.conversations) >= 32:
            self.conversations.pop(next(iter(self.conversations)))
        self.conversations.setdefault(peer.id, deque(maxlen=300)).append((author, text))
        current = self.selected_peer()
        if current and current.id == peer.id:
            self.render_chat(peer)
        elif author != "我":
            self.status.set(f"{peer.name}：有新消息，选择设备查看")

    def render_chat(self, peer):
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        for author, text in self.conversations.get(peer.id, []):
            self.transcript.insert("end", author + "\n", "author")
            self.transcript.insert("end", text + "\n\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def refresh_chat_controls(self):
        peer = self.selected_peer()
        session = self.sessions.get(peer.id) if peer else None
        available = bool(self.node and peer and not (session and session.get("busy")))
        self.message_button.configure(state="normal" if available else "disabled")

    def connect_chat(self, peer):
        if not self.node or not peer:
            raise ValueError("请先选择在线设备")
        if peer.id in self.sessions:
            return self.sessions[peer.id]
        node = self.node
        remembered = node.trust.check(peer)
        session = {
            "peer": peer,
            "outgoing": True,
            "cancel": threading.Event(),
            "confirmed": threading.Event(),
            "ready": False,
            "id": None,
            "cursor": 0,
        }
        if remembered:
            session["confirmed"].set()
        self.sessions[peer.id] = session
        self.refresh_chat_controls()

        def worker():
            try:
                verification_shown = not remembered
                hello = peer_request(peer, "GET", "/peer/hello", source=node.host)
                if hello.get("id") != peer.id or hello.get("remembered_chat") is not True:
                    raise ValueError("在线直聊需要双方升级到邻传 0.5.0 或更新版本")
                offer = node.begin(peer, [], chat=True, automatic=True)
                session.update(offer)
                if not remembered:
                    node.emit("chat_verify", session=session)
                deadline = time.monotonic() + 120
                while not session["cancel"].is_set() and not node.closed.is_set():
                    result = peer_request(
                        peer,
                        "GET",
                        f"/peer/chat/{offer['id']}/0",
                        secret=offer["secret"],
                        source=node.host,
                    )
                    if result["status"] not in {"pending", "accepted"}:
                        raise ValueError("对方取消连接或已离线")
                    if result["status"] == "pending" and not verification_shown:
                        session["confirmed"].clear()
                        node.emit("chat_verify", session=session)
                        verification_shown = True
                    if session["confirmed"].is_set() and result["status"] == "accepted":
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("首次设备核对超时，请重试")
                    session["cancel"].wait(0.4)
                if session["cancel"].is_set() or node.closed.is_set():
                    return
                peer_request(
                    peer,
                    "POST",
                    f"/peer/chat/{offer['id']}/activate",
                    {},
                    offer["secret"],
                    node.host,
                )
                node.trust.remember(peer)
                node.emit("chat_ready", session=session)
                while not session["cancel"].is_set() and not node.closed.is_set():
                    result = peer_request(
                        peer,
                        "GET",
                        f"/peer/chat/{offer['id']}/{session['cursor']}",
                        secret=offer["secret"],
                        source=node.host,
                    )
                    if result["status"] != "active":
                        raise ValueError("设备连接已过期，下次发送会自动重连")
                    if result["oldest"] > session["cursor"] + 1:
                        node.emit("chat_gap", session=session)
                    for item in result["messages"]:
                        session["cursor"] = item["seq"]
                        if item["author"] == "receiver":
                            node.emit("chat_reply", session=session, text=item["text"])
                    session["cancel"].wait(0.5)
            except Exception as error:
                node.emit("chat_failed", session=session, text=str(error))
            finally:
                if session.get("id"):
                    try:
                        peer_request(
                            peer,
                            "POST",
                            f"/peer/chat/{session['id']}/close",
                            {},
                            session["secret"],
                            node.host,
                            timeout=2,
                        )
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()
        return session

    def end_chat(self, peer_id):
        session = self.sessions.pop(peer_id, None)
        if session:
            session["cancel"].set()
            if not session["outgoing"] and self.node:
                self.node.chat.close(session["id"])
            dialog = self.chat_dialogs.pop(session.get("id"), None)
            if dialog:
                dialog.destroy()
        self.refresh_chat_controls()

    def chat_verify(self, session):
        if session["cancel"].is_set():
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("首次确认设备")
        dialog.geometry("420x190")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"首次连接 {session['peer'].name}", wraplength=380).pack(anchor="w")
        ttk.Label(
            frame, text="当面或电话核对双方验证码。确认后记住设备，以后无需重复。", wraplength=380
        ).pack(anchor="w", pady=8)
        ttk.Label(frame, text=session["code"], font=("Consolas", 18, "bold")).pack()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=10)
        self.chat_dialogs[session["id"]] = dialog

        def decide(accepted):
            try:
                if accepted:
                    if session["outgoing"]:
                        session["confirmed"].set()
                    else:
                        self.node.chat.decide(session["id"], True)
                        self.status.set("等待对方完成首次设备确认…")
                else:
                    self.end_chat(session["peer"].id)
                self.refresh_chat_controls()
            except ValueError as error:
                self.status.set(str(error))
                self.end_chat(session["peer"].id)
            finally:
                self.chat_dialogs.pop(session["id"], None)
                if dialog.winfo_exists():
                    dialog.destroy()

        ttk.Button(buttons, text="取消", command=lambda: decide(False)).pack(side="left")
        ttk.Button(buttons, text="一致，记住设备", command=lambda: decide(True)).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: decide(False))

    def send_message(self, event=None):
        peer = self.selected_peer()
        session = self.sessions.get(peer.id) if peer else None
        if not peer or not self.node or (session and session.get("busy")):
            return "break"
        try:
            self.node.trust.check(peer)
        except ValueError as error:
            self.status.set(str(error))
            return "break"
        text = self.composer.get("1.0", "end-1c")
        try:
            text = message_text(text)
        except ValueError as error:
            self.status.set(str(error))
            return "break"
        try:
            session = session or self.connect_chat(peer)
        except ValueError as error:
            self.status.set(str(error))
            return "break"
        if not session["ready"]:
            session["pending_text"] = text
            session["busy"] = True
            self.refresh_chat_controls()
            self.status.set("正在连接设备；首次连接请核对验证码，完成后发送此消息")
            return "break"
        self.deliver_message(session, text)
        return "break"

    def deliver_message(self, session, text):
        peer = session["peer"]
        session["busy"] = True
        self.refresh_chat_controls()
        node = self.node

        def worker():
            try:
                mid = secrets.token_hex(16)
                if session["outgoing"]:
                    peer_request(
                        peer,
                        "POST",
                        f"/peer/chat/{session['id']}/send",
                        {"message_id": mid, "text": text},
                        session["secret"],
                        node.host,
                    )
                else:
                    node.chat.append(session["id"], text, mid, "receiver")
                node.emit("chat_sent", session=session, text=text)
            except Exception as error:
                node.emit("chat_send_error", session=session, text=str(error))

        threading.Thread(target=worker, daemon=True).start()

    def flush_pending(self, session):
        text = session.pop("pending_text", None)
        if text is not None:
            self.deliver_message(session, text)
        paths = session.pop("pending_files", None)
        if paths:
            self.send(peer=session["peer"], paths=paths)

    def handle_chat(self, event):
        kind = event["kind"]
        session = event.get("session")
        if session and self.sessions.get(session["peer"].id) is not session:
            return
        if kind == "chat_request":
            channel = event["channel"]
            existing = self.sessions.get(channel.peer.id)
            # Simultaneous first messages use one channel, with the lower device
            # ID initiating. Move queued content with its peer, never the selection.
            if (
                existing
                and not existing["ready"]
                and (not existing["outgoing"] or self.node.id < channel.peer.id)
            ):
                if channel.status == "pending":
                    self.node.chat.decide(channel.id, False)
                else:
                    self.node.chat.close(channel.id)
                return
            pending = {}
            if existing:
                pending = {
                    k: existing[k] for k in ("pending_text", "pending_files") if k in existing
                }
                pending["busy"] = "pending_text" in pending
                self.end_chat(channel.peer.id)
            session = {
                "id": channel.id,
                "peer": channel.peer,
                "code": channel.code,
                "outgoing": False,
                "cancel": threading.Event(),
                "ready": False,
                **pending,
            }
            self.sessions[channel.peer.id] = session
            if channel.status == "pending":
                self.chat_verify(session)
        elif kind == "chat_active":
            channel = event["channel"]
            active = self.sessions.get(channel.peer.id)
            if active and active["id"] == channel.id:
                active["ready"] = True
                self.status.set(f"已连接 {channel.peer.name} · 在线可直接聊天")
                self.flush_pending(active)
        elif kind == "chat_verify":
            self.chat_verify(session)
        elif kind == "chat_ready":
            session["ready"] = True
            self.status.set(f"已连接 {session['peer'].name} · 在线可直接聊天")
            self.flush_pending(session)
        elif kind == "chat_message":
            self.log(event["peer"], event["peer"].name, event["text"])
        elif kind == "chat_reply":
            self.log(session["peer"], session["peer"].name, event["text"])
        elif kind == "chat_sent":
            session["busy"] = False
            self.log(session["peer"], "我", event["text"])
            if self.selected_peer() and self.selected_peer().id == session["peer"].id:
                # Preserve text typed while the network request was running.
                if self.composer.get("1.0", "end-1c") == event["text"]:
                    self.composer.delete("1.0", "end")
        elif kind == "chat_send_error":
            session["busy"] = False
            self.status.set("消息未确认送达：" + event["text"])
        elif kind == "chat_failed":
            self.log(session["peer"], "系统", event["text"])
            self.end_chat(session["peer"].id)
        elif kind == "chat_gap":
            self.log(session["peer"], "系统", "离线期间部分消息已超出缓存，请向对方核实")
        elif kind == "chat_end":
            for peer_id, active in list(self.sessions.items()):
                if active["id"] == event["id"]:
                    self.end_chat(peer_id)
        self.refresh_chat_controls()
