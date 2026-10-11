"""Startup recovery waits for the permission prompt, respects minimize and stops."""

from lan_drop.startup_window import StartupWindow


class Window:
    def __init__(self):
        self.mode = "normal"
        self.lifts = 0
        self.actions = []
        self.callbacks = []

    def state(self):
        return self.mode

    def lift(self):
        self.lifts += 1

    def attributes(self, key, value):
        self.actions.append((key, value))

    def after(self, delay, callback):
        self.callbacks.append(callback)

    def focus_force(self):
        pass

    def winfo_exists(self):
        return True


class Native:
    handle = 1
    title = "explorer"
    open = True

    def foreground(self):
        return self.handle, self.title

    def visible(self, handle):
        return self.open


def test_restore_once_only_after_new_permission_prompt_closes():
    window, native = Window(), Native()
    controller = StartupWindow(window, native)
    native.handle, native.title = 2, "windows 安全中心"
    controller.poll()
    controller.poll()
    assert window.lifts == 0
    native.open = False
    controller.poll()
    assert window.lifts == 1
    window.callbacks[-1]()
    assert window.actions == [("-topmost", True), ("-topmost", False)]


def test_existing_security_window_and_other_apps_do_not_trigger_recovery():
    window, native = Window(), Native()
    native.title = "windows security"
    controller = StartupWindow(window, native)
    controller.poll()
    assert controller.prompt is None
    native.handle, native.title = 3, "text editor"
    controller.poll()
    assert window.lifts == 0 and controller.prompt is None
    controller.remaining = 0
    before = len(window.callbacks)
    controller.poll()
    assert len(window.callbacks) == before


def test_respect_intentional_minimize_and_withdraw():
    window = Window()
    controller = StartupWindow(window, Native())
    for mode in ("iconic", "withdrawn"):
        window.mode = mode
        controller.present()
    assert window.lifts == 0 and window.actions == []
