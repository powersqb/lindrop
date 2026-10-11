"""Browser-free UI; worker threads pass events through queues."""

import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .discovery import Peer, device_name
from .peer import PeerNode, describe_files, peer_request
from .server import local_addresses

GREEN, BG, MUTED = "#26684e", "#f5f8f4", "#78857c"


def human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


class Desktop:
    def __init__(self, args):
        self.args, self.node = args, None
        self.closed, self.sending, self.starting = False, False, False
        self.ui_events = queue.Queue()
        self.send_cancel, self.send_confirm = threading.Event(), threading.Event()
        self.paths, self.peers, self.manual = [], {}, {}
        self.manual_probe_at, self.incoming = 0, None
        self.window = tk.Tk()
        self.window.title("邻传 · 桌面互传 0.2.0")
        self.window.geometry("960x780")
        self.window.minsize(900, 740)
        self.window.configure(bg=BG)
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground="#23382f", font=("Microsoft YaHei", 10))
        style.configure("TButton", font=("Microsoft YaHei", 10), padding=(12, 8))
        style.configure("Green.TButton", background=GREEN, foreground="white")
        style.map("Green.TButton", background=[("active", "#1c513b")])
        style.configure(
            "Treeview",
            font=("Microsoft YaHei", 10),
            rowheight=34,
            background="white",
            fieldbackground="white",
            borderwidth=0,
        )
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9), background="#eaf0e7")
        style.map(
            "Treeview", background=[("selected", "#e0eddf")], foreground=[("selected", "#224d34")]
        )
        body = ttk.Frame(self.window, padding=20)
        body.pack(fill="both", expand=True)
        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text="↔ 邻传", font=("Microsoft YaHei", 24, "bold")).pack(side="left")
        ttk.Label(header, text="桌面互传 · 仅局域网", foreground=MUTED).pack(side="left", padx=18)
        ttk.Button(header, text="打开接收文件夹", command=self.open_folder).pack(side="right")
        profile = ttk.Frame(body)
        profile.pack(fill="x", pady=(16, 12))
        name = args.name or socket.gethostname()
        if not args.name:
            try:
                name = device_name(
                    json.loads((args.data_dir / "profile.json").read_text("utf-8"))["name"]
                )
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.name_var = tk.StringVar(value=name)
        ttk.Label(profile, text="我的设备名").pack(side="left")
        ttk.Entry(profile, textvariable=self.name_var, width=23).pack(side="left", padx=8)
        ttk.Button(profile, text="保存", command=self.rename).pack(side="left")
        addresses = local_addresses() or ["127.0.0.1"]
        self.host_var = tk.StringVar(value=args.host or addresses[0])
        ttk.Label(profile, text="网络").pack(side="left", padx=(20, 8))
        self.network = ttk.Combobox(
            profile, textvariable=self.host_var, values=addresses, width=17, state="readonly"
        )
        self.network.pack(side="left")
        self.network.bind("<<ComboboxSelected>>", self.switch_network)
        self.status = tk.StringVar(value="正在启动加密连接…")
        ttk.Label(body, textvariable=self.status, foreground=GREEN).pack(anchor="w", pady=(0, 14))
        top = ttk.Frame(body)
        top.pack(fill="both", expand=True)
        top.columnconfigure(0, weight=2, minsize=320)
        top.columnconfigure(1, weight=3, minsize=500)
        top.rowconfigure(0, weight=1)
        left = ttk.Frame(top)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        heading = ttk.Frame(left)
        heading.pack(fill="x", pady=(0, 10))
        self.count = tk.StringVar(value="附近设备 · 0")
        ttk.Label(heading, textvariable=self.count, font=("Microsoft YaHei", 12, "bold")).pack(
            side="left"
        )
        ttk.Button(heading, text="刷新", command=self.refresh).pack(side="right")
        self.devices = ttk.Treeview(
            left, columns=("name", "ip"), show="headings", selectmode="browse", height=5
        )
        for key, label, width in (("name", "在线的邻传设备", 160), ("ip", "地址", 135)):
            self.devices.heading(key, text=label)
            self.devices.column(key, width=width, minwidth=110)
        self.devices.pack(fill="both", expand=True)
        self.devices.bind("<<TreeviewSelect>>", self.select_peer)
        ttk.Label(
            left,
            text="只显示运行邻传的设备。\n两台电脑都打开邻传，稍等几秒即可发现。",
            foreground=MUTED,
            font=("Microsoft YaHei", 9),
        ).pack(anchor="w", pady=10)
        ttk.Button(left, text="找不到设备？手动连接", command=self.manual_connect).pack(anchor="w")
        right = ttk.Frame(top)
        right.grid(row=0, column=1, sticky="nsew")
        self.target = tk.StringVar(value="选择左侧设备，直接发送文件")
        ttk.Label(right, textvariable=self.target, font=("Microsoft YaHei", 12, "bold")).pack(
            anchor="w", pady=(0, 10)
        )
        self.file_list = tk.Listbox(
            right,
            height=3,
            relief="flat",
            bg="white",
            fg="#344b3b",
            font=("Microsoft YaHei", 10),
            selectbackground="#e0eddf",
        )
        self.file_list.pack(fill="x", pady=(0, 8))
        self.file_list.insert("end", "还没有选择文件")
        controls = ttk.Frame(right)
        controls.pack(fill="x")
        self.choose_button = ttk.Button(controls, text="选择文件", command=self.choose)
        self.choose_button.pack(side="left")
        self.send_button = ttk.Button(
            controls, text="发送给选中设备", command=self.send, style="Green.TButton"
        )
        self.send_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(
            controls, text="取消", command=self.cancel_send, state="disabled"
        )
        self.cancel_button.pack(side="left")
        self.send_text = tk.StringVar(value="文件直接保存到对方电脑，不需要浏览器。")
        ttk.Label(right, textvariable=self.send_text, foreground=MUTED, wraplength=490).pack(
            anchor="w", pady=(10, 5)
        )
        self.send_progress = ttk.Progressbar(right, maximum=100)
        self.send_progress.pack(fill="x")
        self.out_code = tk.StringVar()
        verify_row = ttk.Frame(right)
        verify_row.pack(fill="x", pady=(8, 3))
        ttk.Label(
            verify_row, textvariable=self.out_code, font=("Consolas", 16, "bold"), foreground=GREEN
        ).pack(side="left")
        self.verify_button = ttk.Button(
            verify_row, text="一致，继续发送", command=self.confirm_send, state="disabled"
        )
        self.verify_button.pack(side="right")
        receive = ttk.Frame(right)
        receive.pack(fill="x", pady=(18, 10))
        ttk.Label(receive, text="接收请求", font=("Microsoft YaHei", 12, "bold")).pack(anchor="w")
        self.receive_text = tk.StringVar(value="等待其他邻传设备发送文件…")
        ttk.Label(receive, textvariable=self.receive_text, wraplength=490).pack(
            anchor="w", pady=(7, 2)
        )
        self.in_code = tk.StringVar()
        row = ttk.Frame(receive)
        row.pack(fill="x")
        ttk.Label(
            row, textvariable=self.in_code, foreground=GREEN, font=("Consolas", 16, "bold")
        ).pack(side="left")
        self.accept_button = ttk.Button(
            row,
            text="一致，接收",
            style="Green.TButton",
            command=lambda: self.decide(True),
            state="disabled",
        )
        self.accept_button.pack(side="right")
        self.reject_button = ttk.Button(
            row, text="拒绝", command=lambda: self.decide(False), state="disabled"
        )
        self.reject_button.pack(side="right", padx=8)
        self.receive_progress = ttk.Progressbar(receive, maximum=100)
        self.receive_progress.pack(fill="x", pady=(7, 0))
        ttk.Label(body, text="本次传输记录", font=("Microsoft YaHei", 10, "bold")).pack(
            anchor="w", pady=(6, 7)
        )
        self.history = ttk.Treeview(
            body, columns=("time", "direction", "file", "state"), show="headings", height=2
        )
        for key, label, width in (
            ("time", "时间", 80),
            ("direction", "方向", 140),
            ("file", "文件", 440),
            ("state", "状态", 130),
        ):
            self.history.heading(key, text=label)
            self.history.column(key, width=width, minwidth=60, stretch=key == "file")
        self.history.pack(fill="x")
        ttk.Label(
            body,
            text="HTTPS 加密 · 核对双方验证码后再接收 · 防火墙只允许专用网络",
            font=("Microsoft YaHei", 9),
            foreground=MUTED,
        ).pack(anchor="w", pady=(12, 0))
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
        if self.starting or self.sending or self.incoming:
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
        self.target.set(f"发送给 {peer.name}" if peer else "选择左侧设备，直接发送文件")

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
                    node.emit("sent", name=item["name"], peer=peer.name)
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

    def add_history(self, direction, name, state):
        self.history.insert(
            "", 0, values=(datetime.now().strftime("%H:%M"), direction, name, state)
        )
        for row in self.history.get_children()[100:]:
            self.history.delete(row)

    def clear_incoming(self, text):
        self.incoming = None
        self.receive_text.set(text)
        self.in_code.set("")
        self.accept_button.configure(state="disabled")
        self.reject_button.configure(state="disabled", text="拒绝")

    def handle(self, event):
        kind = event["kind"]
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
            self.add_history("发送 → " + event["peer"], event["name"], "已完成")
        elif kind == "send_end":
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
            self.incoming = event["id"]
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
            self.add_history("接收 ← " + event["peer"], event["name"], "已保存并校验")
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
        self.update_peers()
        self.window.after(100, self.tick)

    def close(self):
        self.closed = True
        self.send_cancel.set()
        if self.node:
            self.node.close()
        self.window.destroy()


def run(args):
    app = Desktop(args)
    app.window.mainloop()
