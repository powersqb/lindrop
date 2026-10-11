"""Manual Windows OLE drag test; sends only a synthetic filename on loopback.

Run from the repository: .venv\Scripts\python.exe scripts\Verify-NativeDrop.py
Drag the bottom test button into the chat panel. Neither side auto-accepts.
Close the window after checking outputs/native-drop-test/result.json.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tkinter import ttk  # noqa: E402

from tkinterdnd2 import DND_FILES  # noqa: E402

from lan_drop.desktop import Desktop  # noqa: E402
from lan_drop.peer import PeerNode  # noqa: E402

directory = Path(__file__).resolve().parents[1] / "outputs" / "native-drop-test"
directory.mkdir(parents=True, exist_ok=True)
source = directory / "拖拽 测试文件.txt"
source.write_text("Synthetic local drag test. No user files.", encoding="utf-8")
receiver = PeerNode("127.0.0.1", 0, directory / "receiver", "本机测试设备", discover=False)
app = Desktop(
    SimpleNamespace(host="127.0.0.1", port=0, data_dir=directory / "sender", name="拖拽测试")
)
button = ttk.Button(app.window, text="拖拽测试：将此文件拖进右侧聊天区域")
button.pack(side="bottom", fill="x")
button.drag_source_register(1, DND_FILES)
button.dnd_bind("<<DragInitCmd>>", lambda _: ("copy", DND_FILES, (str(source),)))


def inspect():
    if app.closed:
        return
    if app.node and not app.devices.selection():
        peer = receiver.as_peer()
        app.manual[peer.id] = peer
        app.update_peers()
        app.devices.selection_set(peer.id)
        app.select_peer()
    if receiver.protocol.offers:
        offer = next(iter(receiver.protocol.offers.values()))
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "native_drop_received": True,
                    "file": offer.files[0]["name"],
                    "status": offer.status,
                    "saved_files": len(list(receiver.received.iterdir())),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    app.window.after(200, inspect)


app.window.after(200, inspect)
try:
    app.window.mainloop()
finally:
    receiver.close()
