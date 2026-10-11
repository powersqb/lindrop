"""Browser-free UI; worker threads pass events through queues."""

import json
import os
import queue
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

from .chat_ui import ChatUI
from .discovery import Peer, device_name
from .layout import build
from .peer import PeerNode, describe_files, peer_request

GREEN, BG, MUTED = "#26684e", "#f5f8f4", "#78857c"


def human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


class Desktop(ChatUI):
    def __init__(self, args):
        self.args, self.node = args, None
        self.closed, self.sending, self.starting = False, False, False
        self.ui_events = queue.Queue()
        self.send_cancel, self.send_confirm = threading.Event(), threading.Event()
        self.paths, self.peers, self.manual = [], {}, {}
        self.manual_probe_at, self.incoming = 0, None
        self.sessions, self.conversations, self.chat_dialogs = {}, {}, {}
        self.displayed_peer = None
        self.file_peers = {}
        build(self, args)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.start()
        self.window.after(100, self.tick)

    def start(self):
        self.starting = True
        host, name = self.host_var.get(), self.name_var.get()
        self.network.configure(state="disabled")

        def worker():
            try:
                node = PeerNode(host, self.args.port, self.args.data_dir, name)
                if self.closed:
                    node.close()
                else:
                    self.ui_events.put({"kind": "ready", "node": node})
            except Exception as error:
                self.ui_events.put({"kind": "startup_error", "text": str(error)})

        threading.Thread(target=worker, daemon=True).start()

    def rename(self):
        try:
            name = device_name(self.name_var.get())
            if not self.node:
                raise ValueError("请等待应用启动完成")
            self.node.name = name
            (self.args.data_dir / "profile.json").write_text(
                json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8"
            )
            self.status.set("设备名已保存，其他邻传窗口将自动更新")
            self.refresh()
        except (ValueError, OSError) as error:
            messagebox.showerror("设备名", str(error), parent=self.window)

    def switch_network(self, _=None):
        if self.starting or self.sending or self.incoming or self.sessions:
            if self.node:
                self.host_var.set(self.node.host)
            messagebox.showinfo("切换网络", "请等待当前传输完成再切换网络", parent=self.window)
            return
        if self.node:
            self.node.close()
            self.node = None
        self.peers.clear()
        self.manual.clear()
        rows = self.devices.get_children()
        if rows:
            self.devices.delete(*rows)
        self.status.set("正在切换网络…")
        self.start()

    def refresh(self):
        if self.node and self.node.discovery:
            try:
                self.node.discovery.announce()
            except OSError:
                self.status.set("自动发现不可用，请检查专用网络或使用手动连接")

    def select_peer(self, _=None):
        selected = self.devices.selection()
        peer = self.peers.get(selected[0]) if selected else None
        self.target.set(f"{peer.name}" if peer else "选择设备开始")
        identifier = peer.id if peer else None
        if identifier != self.displayed_peer:
            self.displayed_peer = identifier
            self.composer.delete("1.0", "end")
            if peer:
                self.render_chat(peer)
        self.refresh_chat_controls()

    def choose(self):
        paths = filedialog.askopenfilenames(title="选择要发送的文件", parent=self.window)
        if paths:
            self.paths = list(paths)
            self.file_list.delete(0, "end")
            for path in paths:
                self.file_list.insert("end", Path(path).name)

    def send(self):
        selected = self.devices.selection()
        peer = self.peers.get(selected[0]) if selected else None
        if not self.node or not peer or not self.paths or self.sending:
            messagebox.showinfo("发送文件", "请先选择在线设备和要发送的文件", parent=self.window)
            return
        paths, node = list(self.paths), self.node
        self.file_peers[peer.id] = peer
        self.sending = True
        self.send_cancel.clear()
        self.send_confirm.clear()
        self.send_button.configure(state="disabled")
        self.choose_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.send_progress["value"] = 0
        self.send_text.set("正在校验文件…")

        def worker():
            offer = None
            try:
                files = describe_files(
                    paths,
                    self.send_cancel,
                    lambda name, done, total: node.emit(
                        "send_progress", phase="校验", name=name, done=done, total=total
                    ),
                )
                if self.send_cancel.is_set():
                    raise InterruptedError("发送已取消")
                offer = node.begin(peer, files)
                node.emit("outgoing_offer", code=offer["code"], name=peer.name)
                deadline = time.monotonic() + 120
                while not self.send_confirm.wait(0.2):
                    if self.send_cancel.is_set() or node.closed.is_set():
                        raise InterruptedError("发送已取消")
                    if time.monotonic() > deadline:
                        raise TimeoutError("验证码核对超时，请重新发送")
                node.emit("send_wait", text="等待对方确认接收…")
                token = node.wait_accepted(peer, offer, self.send_cancel)
                for index, (path, item) in enumerate(zip(paths, files, strict=True)):
                    node.send_file(
                        peer,
                        offer,
                        token,
                        index,
                        path,
                        item,
                        self.send_cancel,
                        lambda name, done, total: node.emit(
                            "send_progress", phase="发送", name=name, done=done, total=total
                        ),
                    )
                    node.emit("sent", name=item["name"], peer=peer.name, peer_id=peer.id)
                node.emit("send_end", text=f"已发送 {len(files)} 个文件给 {peer.name}", ok=True)
            except Exception as error:
                if offer:
                    try:
                        node.cancel(peer, offer)
                    except Exception:
                        pass
                node.emit("send_end", text=str(error), ok=False)

        threading.Thread(target=worker, daemon=True).start()

    def confirm_send(self):
        self.send_confirm.set()
        self.verify_button.configure(state="disabled")

    def cancel_send(self):
        self.send_cancel.set()
        self.send_text.set("正在取消发送…")
        self.verify_button.configure(state="disabled")

    def decide(self, accepted):
        if self.node and self.incoming:
            try:
                if str(self.accept_button["state"]) == "disabled" and not accepted:
                    self.node.protocol.abort(self.incoming)
                else:
                    self.node.protocol.decide(self.incoming, accepted)
            except ValueError as error:
                messagebox.showerror("接收文件", str(error), parent=self.window)

    def manual_connect(self):
        if not self.node:
            return
        value = simpledialog.askstring(
            "手动连接", "对方窗口中的 IPv4 地址（可带 :端口）：", parent=self.window
        )
        if not value:
            return
        node = self.node

        def worker():
            try:
                parts = value.strip().split(":")
                if len(parts) > 2:
                    raise ValueError("只支持 IPv4")
                peer = node.manual_peer(parts[0], int(parts[1]) if len(parts) == 2 else 8765)
                if peer.id == node.id:
                    raise ValueError("这是本机地址，请输入另一台电脑的地址")
                node.emit("manual_peer", peer=peer)
            except Exception as error:
                node.emit("notice", text=f"手动连接失败：{error}")

        threading.Thread(target=worker, daemon=True).start()

    def open_folder(self):
        directory = self.args.data_dir / "received"
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(directory))

    def add_history(self, direction, name, state, peer_id=None):
        peer = self.file_peers.get(peer_id) if peer_id else self.selected_peer()
        if peer:
            self.log(peer, "文件 · " + direction, name + " · " + state)
        else:
            self.status.set(name + " · " + state)

    def settings(self):
        self.settings_window.deiconify()
        self.settings_window.lift()

    def drop_files(self, event):
        if self.sending:
            self.status.set("当前正在发送，请稍后拖入文件")
            return "none"
        try:
            paths = list(self.window.tk.splitlist(event.data))
            if not paths or len(paths) > 50 or any(not Path(p).is_file() for p in paths):
                raise ValueError("请拖入 1 至 50 个普通文件；暂不支持文件夹")
            if not self.node or not self.selected_peer():
                raise ValueError("请先选择在线设备，再拖入文件")
            self.paths = paths
            self.file_list.delete(0, "end")
            self.file_list.insert("end", "、".join(Path(p).name for p in paths))
            self.send()
            return "copy"
        except (ValueError, OSError) as error:
            self.status.set(str(error))
            return "none"

    def clear_incoming(self, text):
        self.incoming = None
        self.receive_card.pack_forget()
        self.status.set(text)
        self.receive_text.set(text)
        self.in_code.set("")
        self.accept_button.configure(state="disabled")
        self.reject_button.configure(state="disabled", text="拒绝")

    def handle(self, event):
        kind = event["kind"]
        if kind.startswith("chat_"):
            self.handle_chat(event)
            return
        if kind == "ready":
            self.node = event["node"]
            self.starting = False
            self.network.configure(state="readonly")
            self.status.set(f"在线 · {self.node.host}:{self.node.port} · 正在发现附近的邻传设备")
        elif kind == "startup_error":
            self.starting = False
            self.network.configure(state="readonly")
            self.status.set("启动失败，可切换网络后重试")
            messagebox.showerror("邻传启动失败", event["text"], parent=self.window)
        elif kind == "notice":
            self.status.set(event["text"])
        elif kind in {"manual_peer", "manual_seen"}:
            peer = event["peer"]
            if peer.id in self.manual or (kind == "manual_peer" and len(self.manual) < 16):
                self.manual[peer.id] = peer
        elif kind == "outgoing_offer":
            self.send_card.pack(fill="x", before=self.composer)
            self.out_code.set(event["code"])
            self.send_text.set(f"请与 {event['name']} 核对双方窗口中的验证码，然后继续发送。")
            self.verify_button.configure(state="normal")
        elif kind == "send_wait":
            self.send_text.set(event["text"])
        elif kind == "send_progress":
            percent = 100 * event["done"] / max(event["total"], 1)
            self.send_progress["value"] = percent
            self.send_text.set(f"{event['phase']} · {event['name']} · {percent:.0f}%")
        elif kind == "sent":
            self.add_history("发送 → " + event["peer"], event["name"], "已完成", event["peer_id"])
        elif kind == "send_end":
            self.send_card.pack_forget()
            self.sending = False
            self.send_text.set(event["text"])
            self.out_code.set("")
            self.send_button.configure(state="normal")
            self.choose_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            self.verify_button.configure(state="disabled")
            if not event["ok"]:
                self.add_history("发送", "本次传输", event["text"])
        elif kind == "incoming":
            self.receive_card.pack(fill="x", before=self.composer)
            self.incoming = event["id"]
            self.file_peers[event["peer"].id] = event["peer"]
            total = sum(item["size"] for item in event["files"])
            names = "、".join(item["name"] for item in event["files"][:3])
            names = names[:75] + ("…" if len(names) > 75 else "")
            self.receive_text.set(
                f"{event['name']} ({event['host']}) 发来 {len(event['files'])} 个文件，"
                f"共 {human_size(total)}：{names}"
            )
            self.in_code.set(event["code"])
            self.accept_button.configure(state="normal")
            self.reject_button.configure(state="normal", text="拒绝")
            self.receive_progress["value"] = 0
            self.window.bell()
        elif kind == "incoming_decided":
            self.accept_button.configure(state="disabled")
            if event["accepted"]:
                self.reject_button.configure(text="停止接收")
                self.receive_text.set("已确认接收，等待发送方核对验证码…")
            else:
                self.clear_incoming("已拒绝接收")
        elif kind == "receive_progress":
            self.receive_progress["value"] = 100 * event["done"] / max(event["total"], 1)
            self.receive_text.set(
                f"接收 · {event['name']} · {human_size(event['done'])} / "
                f"{human_size(event['total'])}"
            )
        elif kind == "received":
            self.add_history(
                "接收 ← " + event["peer"], event["name"], "已保存并校验", event["peer_id"]
            )
            if event["finished"]:
                self.receive_progress["value"] = 100
                self.clear_incoming("接收完成，可点击“打开接收文件夹”查看")
        elif kind in {"incoming_end", "receive_error"} and self.incoming == event["id"]:
            self.clear_incoming(event["text"])

    def update_peers(self):
        if not self.node:
            return
        peers = self.node.discovery.snapshot() if self.node.discovery else []
        now = time.monotonic()
        self.manual = {key: peer for key, peer in self.manual.items() if now - peer.seen < 8}
        self.peers = {peer.id: peer for peer in [*peers, *self.manual.values()]}
        for key in self.devices.get_children():
            if key not in self.peers:
                self.devices.delete(key)
        for peer in sorted(self.peers.values(), key=lambda item: item.name):
            values = (peer.name, peer.host)
            if self.devices.exists(peer.id):
                if tuple(self.devices.item(peer.id, "values")) != values:
                    self.devices.item(peer.id, values=values)
            else:
                self.devices.insert("", "end", iid=peer.id, values=values)
        self.count.set(f"附近设备 · {len(self.peers)}")
        self.select_peer()
        if now > self.manual_probe_at:
            self.manual_probe_at = now + 3
            node, manual = self.node, list(self.manual.values())

            def probe():
                for peer in manual:
                    try:
                        result = peer_request(
                            peer, "GET", "/peer/hello", source=node.host, timeout=2
                        )
                        if result.get("id") == peer.id:
                            node.emit(
                                "manual_seen",
                                peer=Peer(
                                    peer.id,
                                    device_name(result["name"]),
                                    peer.host,
                                    peer.port,
                                    peer.fingerprint,
                                    time.monotonic(),
                                ),
                            )
                    except Exception:
                        pass

            if manual:
                threading.Thread(target=probe, daemon=True).start()

    def tick(self):
        if self.closed:
            return
        for events in (self.ui_events, self.node.events if self.node else queue.Queue()):
            for _ in range(250):
                try:
                    self.handle(events.get_nowait())
                except queue.Empty:
                    break
        if self.node:
            self.node.protocol.sweep()
            self.node.chat.sweep()
        self.update_peers()
        self.window.after(100, self.tick)

    def close(self):
        self.closed = True
        for peer_id in list(self.sessions):
            self.end_chat(peer_id)
        self.send_cancel.set()
        if self.node:
            self.node.close()
        self.window.destroy()


def run(args):
    app = Desktop(args)
    app.window.mainloop()
