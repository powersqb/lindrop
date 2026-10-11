"""Collect Tk reference cycles on the owning thread between integration tests."""

import gc
import time
from types import SimpleNamespace

import pytest

from lan_drop.desktop import Desktop


@pytest.fixture(autouse=True)
def main_thread_collection():
    yield
    # Tk Variable finalizers must not be triggered by a later HTTP worker's GC.
    gc.collect()


@pytest.fixture
def desktop(tmp_path):
    app = Desktop(SimpleNamespace(host="127.0.0.1", port=0, data_dir=tmp_path / "ui", name="UI A"))
    app.window.withdraw()
    try:
        deadline = time.monotonic() + 5
        while app.node is None and time.monotonic() < deadline:
            app.window.update()
            time.sleep(0.02)
        assert app.node is not None
        yield app
    finally:
        app.close()
