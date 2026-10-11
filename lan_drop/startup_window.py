"""Bounded startup visibility recovery; never interact with security dialogs.

Monitor only the foreground title during startup. If a newly observed Windows
network permission window disappears, present our own window once. No firewall
rule, permission choice, input injection or global foreground setting changes.
"""

import ctypes
import os
from ctypes import wintypes

PERMISSION_TITLES = {
    "windows security",
    "windows defender firewall",
    "windows firewall",
    "windows 安全中心",
    "windows 安全性",
    "windows 安全",
    "windows defender 防火墙",
    "windows 防火墙",
    "windows 安全性警示",
    "windows 安全警报",
}


class NativeWindows:
    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetForegroundWindow.restype = wintypes.HWND
        self.api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.IsWindowVisible.argtypes = [wintypes.HWND]
        self.api.IsWindow.argtypes = [wintypes.HWND]

    def foreground(self):
        handle = self.api.GetForegroundWindow()
        text = ctypes.create_unicode_buffer(256)
        self.api.GetWindowTextW(handle, text, len(text))
        return handle, text.value.casefold().strip()

    def visible(self, handle):
        return bool(self.api.IsWindow(handle) and self.api.IsWindowVisible(handle))


class StartupWindow:
    def __init__(self, window, native=None):
        self.window = window
        self.native = (
            native if native is not None else (NativeWindows() if os.name == "nt" else None)
        )
        self.prompt = None
        self.remaining = 360  # 90 seconds; never monitor for the rest of the session.
        self.ignored = self.native.foreground()[0] if self.native else None

    def present(self):
        if self.window.state() in {"withdrawn", "iconic"}:
            return  # Respect an intentional user minimize/close or test window.
        self.window.lift()
        # Briefly make only our own window visible, then release topmost state.
        # Windows may refuse foreground focus; do not change OS focus policies.
        self.window.attributes("-topmost", True)
        self.window.after(250, lambda: self.window.attributes("-topmost", False))
        self.window.focus_force()

    def poll(self):
        if not self.native or not self.window.winfo_exists() or self.remaining <= 0:
            return
        self.remaining -= 1
        if self.prompt is not None:
            if not self.native.visible(self.prompt):
                self.present()
                return  # Recover once, not on subsequent device events.
        else:
            handle, title = self.native.foreground()
            if handle != self.ignored and title in PERMISSION_TITLES:
                self.prompt = handle
        self.window.after(250, self.poll)
