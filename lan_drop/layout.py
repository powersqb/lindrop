"""Compact Tk layout. Keep widget construction separate from network behavior."""

import json
import socket
import tkinter as tk
from tkinter import ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from . import __version__
from .discovery import device_name
from .server import local_addresses


def build(app, args):
    app.window = tk_root = TkinterDnD.Tk()
    tk_root.title(f"邻传 {__version__} · 局域网工具")
    tk_root.geometry("720x580")
    tk_root.minsize(640, 520)
    style = ttk.Style(tk_root)
    style.theme_use("clam")
    style.configure("TFrame", background="#f5f8f4")
    style.configure("TLabel", background="#f5f8f4", font=("Microsoft YaHei", 9))
    style.configure("TButton", font=("Microsoft YaHei", 9), padding=(8, 4))
    style.configure("Green.TButton", background="#26684e", foreground="white")
    style.map("Green.TButton", background=[("active", "#1c513b")])
    style.configure(
        "Treeview",
        font=("Microsoft YaHei", 10),
        rowheight=32,
        background="white",
        fieldbackground="white",
    )
    style.map(
        "Treeview", background=[("selected", "#dcebdd")], foreground=[("selected", "#224d34")]
    )
    body = ttk.Frame(tk_root, padding=12)
    body.pack(fill="both", expand=True)
    header = ttk.Frame(body)
    header.pack(fill="x", pady=(0, 8))
    ttk.Label(header, text="↔ 邻传", font=("Microsoft YaHei", 16, "bold")).pack(side="left")
    ttk.Label(header, text="仅局域网", foreground="#78857c").pack(side="left", padx=10)
    ttk.Button(header, text="设置", command=app.settings).pack(side="right")
    ttk.Button(header, text="接收文件夹", command=app.open_folder).pack(side="right", padx=6)
    main = ttk.Frame(body)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, minsize=155)
    main.columnconfigure(1, weight=1)
    main.rowconfigure(0, weight=1)
    left = ttk.Frame(main)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    app.count = tk.StringVar(value="附近设备 · 0")
    ttk.Label(left, textvariable=app.count).pack(anchor="w", pady=(0, 6))
    app.devices = ttk.Treeview(
        left,
        columns=("name", "ip"),
        displaycolumns=("name",),
        show="tree",
        selectmode="browse",
        height=6,
    )
    app.devices.column("#0", width=0, stretch=False)
    app.devices.configure(show="headings")
    app.devices.heading("name", text="在线设备")
    app.devices.column("name", width=155, minwidth=130, stretch=False)
    app.devices.pack(fill="both", expand=True)
    app.devices.bind("<<TreeviewSelect>>", app.select_peer)
    ttk.Button(left, text="刷新设备", command=app.refresh).pack(fill="x", pady=(8, 4))
    ttk.Button(left, text="手动连接", command=app.manual_connect).pack(fill="x")
    ttk.Label(left, text="两端打开邻传即可发现", foreground="#78857c", wraplength=150).pack(
        anchor="w", pady=8
    )
    right = ttk.Frame(main)
    right.grid(row=0, column=1, sticky="nsew")
    heading = ttk.Frame(right)
    heading.pack(fill="x", pady=(0, 6))
    app.target = tk.StringVar(value="选择设备开始")
    ttk.Label(
        heading, textvariable=app.target, font=("Microsoft YaHei", 10, "bold"), wraplength=280
    ).pack(side="left")
    transcript_frame = ttk.Frame(right)
    transcript_frame.pack(fill="both", expand=True)
    app.transcript = tk.Text(
        transcript_frame,
        height=1,
        width=30,
        state="disabled",
        wrap="word",
        relief="flat",
        padx=10,
        pady=10,
        font=("Microsoft YaHei", 10),
        bg="white",
        fg="#243a2e",
    )
    scroll = ttk.Scrollbar(transcript_frame, command=app.transcript.yview)
    scroll.pack(side="right", fill="y")
    app.transcript.configure(yscrollcommand=scroll.set)
    app.transcript.pack(fill="both", expand=True)
    app.transcript.tag_configure(
        "author", foreground="#26684e", font=("Microsoft YaHei", 9, "bold")
    )
    app.transcript.configure(state="normal")
    app.transcript.insert(
        "end",
        "选择左侧在线设备，直接发送消息或文件。\n\n首次连接核对一次验证码，以后记住设备。\n拖入文件即可发送，对方决定是否接收。",
    )
    app.transcript.configure(state="disabled")
    app.composer = tk.Text(
        right, height=3, relief="flat", wrap="word", font=("Microsoft YaHei", 10), padx=8, pady=6
    )
    app.composer.pack(fill="x", pady=(6, 4))
    app.composer.bind("<Return>", app.send_message)
    app.composer.bind("<Shift-Return>", lambda _: None)
    bottom = ttk.Frame(right)
    bottom.pack(fill="x")
    ttk.Label(bottom, text="Enter 发送 · Shift+Enter 换行", foreground="#78857c").pack(side="left")
    app.message_button = ttk.Button(
        bottom, text="发送消息", command=app.send_message, state="disabled", style="Green.TButton"
    )
    app.message_button.pack(side="right")
    tools = ttk.Frame(right)
    tools.pack(fill="x", pady=(8, 4))
    app.choose_button = ttk.Button(tools, text="选择文件", command=app.choose)
    app.choose_button.pack(side="left")
    app.send_button = ttk.Button(tools, text="发送文件", command=app.send)
    app.send_button.pack(side="left", padx=5)
    app.cancel_button = ttk.Button(tools, text="取消", command=app.cancel_send, state="disabled")
    app.cancel_button.pack(side="right")
    app.file_list = tk.Listbox(right, height=1, relief="flat", font=("Microsoft YaHei", 9))
    app.file_list.pack(fill="x")
    app.file_list.insert("end", "支持多文件拖拽 · 不支持文件夹")
    app.send_text = tk.StringVar(value="")
    ttk.Label(right, textvariable=app.send_text, wraplength=440, foreground="#78857c").pack(
        anchor="w"
    )
    app.send_progress = ttk.Progressbar(right, maximum=100)
    app.send_progress.pack(fill="x", pady=(2, 4))
    app.receive_card = ttk.Frame(right)
    app.receive_text = tk.StringVar()
    ttk.Label(app.receive_card, textvariable=app.receive_text, wraplength=430).pack(anchor="w")
    row = ttk.Frame(app.receive_card)
    row.pack(fill="x")
    app.in_code = tk.StringVar()
    ttk.Label(row, textvariable=app.in_code, font=("Consolas", 12, "bold")).pack(side="left")
    app.accept_button = ttk.Button(
        row, text="接收", command=lambda: app.decide(True), state="disabled"
    )
    app.accept_button.pack(side="right")
    app.reject_button = ttk.Button(
        row, text="拒绝", command=lambda: app.decide(False), state="disabled"
    )
    app.reject_button.pack(side="right", padx=4)
    app.receive_progress = ttk.Progressbar(app.receive_card, maximum=100)
    app.receive_progress.pack(fill="x", pady=3)
    app.status = tk.StringVar(value="正在启动加密连接…")
    ttk.Label(body, textvariable=app.status, foreground="#26684e", wraplength=680).pack(
        anchor="w", pady=(8, 0)
    )
    # Native OS paths arrive as a Tcl list, NOT a space-separated string.
    for widget in (tk_root, app.transcript, app.composer, app.file_list):
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", app.drop_files)

    app.settings_window = settings = tk.Toplevel(tk_root)
    settings.title("邻传设置")
    settings.geometry("380x285")
    settings.withdraw()
    settings.protocol("WM_DELETE_WINDOW", settings.withdraw)
    frame = ttk.Frame(settings, padding=16)
    frame.pack(fill="both", expand=True)
    name = args.name or socket.gethostname()
    if not args.name:
        try:
            name = device_name(
                json.loads((args.data_dir / "profile.json").read_text("utf-8"))["name"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            pass
    app.name_var = tk.StringVar(value=name)
    ttk.Label(frame, text="我的设备名").pack(anchor="w")
    ttk.Entry(frame, textvariable=app.name_var).pack(fill="x", pady=4)
    ttk.Button(frame, text="保存名称", command=app.rename).pack(anchor="e")
    addresses = local_addresses() or ["127.0.0.1"]
    app.host_var = tk.StringVar(value=args.host or addresses[0])
    ttk.Label(frame, text="使用的局域网 IPv4").pack(anchor="w", pady=(10, 3))
    app.network = ttk.Combobox(frame, textvariable=app.host_var, values=addresses, state="readonly")
    app.network.pack(fill="x")
    app.network.bind("<<ComboboxSelected>>", app.switch_network)

    ttk.Button(frame, text="软件更新 · GitHub", command=app.show_updates).pack(
        anchor="e", pady=(12, 0)
    )
    ttk.Button(frame, text="设备身份 / 忘记设备", command=app.show_identity).pack(
        anchor="e", pady=4
    )
