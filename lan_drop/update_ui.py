"""Manual update UI; the window thread consumes worker results via ui_events."""

import os
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import __version__
from .installer import prepare, start_helper
from .updates import RELEASES_URL, check_latest, download


class UpdateUI:
    def show_updates(self):
        existing = getattr(self, "update_window", None)
        if existing and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            return
        self.update_window = dialog = tk.Toplevel(self.window)
        dialog.title("邻传 · 软件更新")
        dialog.geometry("460x350")
        dialog.minsize(420, 320)
        dialog.transient(self.window)
        self.update_cancel = threading.Event()
        self.update_release, self.update_path = None, None
        self.update_busy = False
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"当前版本 {__version__}", font=("Microsoft YaHei", 12, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            frame, text="仅在你点击检查或下载时连接 GitHub；不会上传文件。", wraplength=420
        ).pack(anchor="w", pady=(8, 4))
        self.update_status = tk.StringVar(value="点击检查更新，或打开 GitHub 发布页。")
        ttk.Label(frame, textvariable=self.update_status, wraplength=420).pack(anchor="w", pady=6)
        self.update_notes = tk.Text(
            frame,
            height=1,
            state="disabled",
            wrap="word",
            relief="flat",
            font=("Microsoft YaHei", 9),
            padx=8,
            pady=6,
        )
        self.update_notes.pack(fill="both", expand=True)
        self.update_progress = ttk.Progressbar(frame, maximum=100)
        self.update_progress.pack(fill="x", pady=8)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.check_button = ttk.Button(row, text="检查更新", command=self.check_updates)
        self.check_button.pack(side="left")
        self.download_button = ttk.Button(
            row, text="下载新版 ZIP", command=self.download_update, state="disabled"
        )
        self.download_button.pack(side="left", padx=5)
        ttk.Button(row, text="GitHub 发布页", command=lambda: webbrowser.open(RELEASES_URL)).pack(
            side="right"
        )
        install_row = ttk.Frame(frame)
        install_row.pack(fill="x", pady=(8, 0))
        self.update_open_button = ttk.Button(
            install_row, text="打开下载文件夹", command=self.open_update_folder, state="disabled"
        )
        self.update_open_button.pack(side="left")
        self.install_button = ttk.Button(
            install_row, text="安装并重启", command=self.install_update, state="disabled"
        )
        self.install_button.pack(side="right")
        ttk.Label(
            frame,
            text="校验后点击安装并重启，自动替换 EXE，保留数据和设备信任。",
            wraplength=420,
            foreground="#78857c",
        ).pack(anchor="w", pady=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", self.close_updates)

    def close_updates(self):
        self.update_cancel.set()
        self.update_window.withdraw()

    def check_updates(self):
        if self.update_busy:
            return
        self.update_busy = True
        self.update_cancel.clear()
        self.update_release = None
        self.update_path = None
        self.check_button.configure(state="disabled")
        self.download_button.configure(state="disabled")
        self.install_button.configure(state="disabled")
        self.update_status.set("正在连接 GitHub 检查版本…")

        def worker():
            try:
                release, newer = check_latest(__version__)
                self.ui_events.put({"kind": "update_checked", "release": release, "newer": newer})
            except Exception as error:
                self.ui_events.put({"kind": "update_error", "text": str(error)})

        threading.Thread(target=worker, daemon=True).start()

    def download_update(self):
        if self.update_busy or not self.update_release:
            return
        self.update_busy = True
        self.update_cancel.clear()
        self.download_button.configure(state="disabled")
        self.check_button.configure(state="disabled")
        self.update_status.set("正在下载并校验更新包；关闭此窗口可取消。")
        release = self.update_release

        def worker():
            try:
                path = download(
                    release,
                    self.args.data_dir / "updates",
                    self.update_cancel,
                    lambda done, total: self.ui_events.put(
                        {"kind": "update_progress", "done": done, "total": total}
                    ),
                )
                self.ui_events.put({"kind": "update_downloaded", "path": path})
            except Exception as error:
                self.ui_events.put({"kind": "update_error", "text": str(error)})

        threading.Thread(target=worker, daemon=True).start()

    def open_update_folder(self):
        if self.update_path:
            os.startfile(str(self.update_path.parent))

    def install_update(self):
        if self.update_busy or not self.update_path or not self.update_release:
            return
        if self.sending or self.incoming or self.starting:
            messagebox.showinfo(
                "软件更新", "请先完成或取消文件传输，再安装更新。", parent=self.window
            )
            return
        if not getattr(sys, "frozen", False):
            self.update_status.set("源码运行请通过 Git 更新；自动替换仅适用于独立 EXE。")
            return
        self.update_busy = True
        self.update_cancel.clear()
        for button in (self.check_button, self.download_button, self.install_button):
            button.configure(state="disabled")
        self.update_status.set("正在准备替换；成功后会自动重启，聊天记录将在重启时清空。")
        archive, release = self.update_path, self.update_release
        arguments = ["--data-dir", str(self.args.data_dir.resolve()), "--port", str(self.args.port)]
        if self.args.host:
            arguments.extend(["--host", self.args.host])
        if self.args.name:
            arguments.extend(["--name", self.args.name])

        def worker():
            manifest = None
            try:
                manifest = prepare(archive, release, sys.executable, arguments)
                if self.update_cancel.is_set():
                    raise InterruptedError("更新已取消")
                start_helper(manifest)
                self.ui_events.put({"kind": "update_install_ready", "manifest": manifest})
            except Exception as error:
                if manifest:
                    (manifest.parent / "cancel").touch()
                self.ui_events.put({"kind": "update_error", "text": str(error)})

        threading.Thread(target=worker, daemon=True).start()

    def handle_update(self, event):
        if self.closed:
            return
        kind = event["kind"]
        if kind == "update_install_ready":
            if self.update_cancel.is_set() or self.sending or self.incoming or self.starting:
                (event["manifest"].parent / "cancel").touch()
                self.update_busy = False
                self.check_button.configure(state="normal")
                self.install_button.configure(state="normal")
                self.update_status.set("安装已取消，请结束文件传输后重试。")
            else:
                self.close()
            return
        if kind == "update_progress":
            self.update_progress["value"] = 100 * event["done"] / event["total"]
            return
        self.update_busy = False
        self.check_button.configure(state="normal")
        if kind == "update_checked":
            release = event["release"]
            if event["newer"]:
                self.update_release = release
                self.download_button.configure(state="normal")
                self.update_status.set(f"发现新版 {release.version}，可以下载校验后的 ZIP。")
            else:
                self.update_status.set(f"没有更高版本；GitHub 最新正式版为 {release.version}。")
            self.update_notes.configure(state="normal")
            self.update_notes.delete("1.0", "end")
            self.update_notes.insert("end", release.notes or "此版本未提供更新说明。")
            self.update_notes.configure(state="disabled")
        elif kind == "update_downloaded":
            self.update_path = event["path"]
            self.update_progress["value"] = 100
            self.update_open_button.configure(state="normal")
            self.install_button.configure(state="normal")
            self.update_status.set("SHA-256 校验通过。点击“安装并重启”自动替换 EXE。")
        elif kind == "update_error":
            self.update_status.set(event["text"])
            if self.update_release:
                self.download_button.configure(state="normal")
            if self.update_path:
                self.install_button.configure(state="normal")
