"""Collect Tk reference cycles on the owning thread between integration tests."""

import gc

import pytest


@pytest.fixture(autouse=True)
def main_thread_collection():
    yield
    # Tk Variable finalizers must not be triggered by a later HTTP worker's GC.
    gc.collect()
