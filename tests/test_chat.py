"""Real HTTPS channels: acceptance, bidirectional text, bounds and revocation."""

import secrets
import threading
import time

import pytest

from lan_drop.chat import message_text
from lan_drop.peer import PeerNode, describe_files, peer_request


@pytest.fixture
def pair(tmp_path):
    a = PeerNode("127.0.0.1", 0, tmp_path / "a", "聊天 A", discover=False)
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", "聊天 B", discover=False)
    try:
        yield a, b
    finally:
        a.close()
        b.close()


def request(a, b, offer, action, data=None, secret=None):
    return peer_request(
        b.as_peer(),
        "GET" if action.isdigit() else "POST",
        f"/peer/chat/{offer['id']}/{action}",
        data,
        secret or offer["secret"],
        a.host,
    )


def test_chat_authorization_delivery_and_close(pair, tmp_path):
    a, b = pair
    offer = a.begin(b.as_peer(), [], chat=True)
    channel = b.chat.channels[offer["id"]]
    assert channel.code == offer["code"]
    assert request(a, b, offer, "0") == {"status": "pending", "messages": [], "oldest": 0}
    body = {"text": "你好\n中文 <script> 是普通文字", "message_id": secrets.token_hex(16)}
    with pytest.raises(ValueError):
        request(a, b, offer, "send", body)
    with pytest.raises(ValueError):
        request(a, b, offer, "0", secret="x" * 43)
    b.chat.decide(channel.id, True)
    with pytest.raises(ValueError):
        b.chat.append(channel.id, "waiting", secrets.token_hex(16), "receiver")
    request(a, b, offer, "activate", {})
    request(a, b, offer, "send", body)
    request(a, b, offer, "send", body)  # retry must not duplicate messages
    b.chat.append(channel.id, "收到 😊", secrets.token_hex(16), "receiver")
    page = request(a, b, offer, "0")
    assert [m["text"] for m in page["messages"]] == [body["text"], "收到 😊"]
    assert request(a, b, offer, "2")["messages"] == []
    # An accepted chat must not block file transfers using the same TLS listener.
    source = tmp_path / "文件.txt"
    source.write_text("payload", encoding="utf-8")
    files = describe_files([source], threading.Event(), lambda *_: None)
    transfer = a.begin(b.as_peer(), files)
    assert b.protocol.offers[transfer["id"]].status == "pending"
    request(a, b, offer, "close", {})
    assert request(a, b, offer, "0")["status"] == "closed"
    with pytest.raises(ValueError):
        request(a, b, offer, "send", body)


def test_chat_expiry_limits_and_rejection(pair):
    a, b = pair
    offer = a.begin(b.as_peer(), [], chat=True)
    b.chat.decide(offer["id"], False)
    assert request(a, b, offer, "0")["status"] == "rejected"
    b.chat.channels[offer["id"]].expires = time.monotonic() - 1
    with pytest.raises(ValueError):
        request(a, b, offer, "0")
    assert offer["id"] not in b.chat.channels
    offer = a.begin(b.as_peer(), [], chat=True)
    b.chat.decide(offer["id"], True)
    request(a, b, offer, "activate", {})
    with pytest.raises(ValueError):
        request(a, b, offer, "send", {"text": "中" * 2000, "message_id": secrets.token_hex(16)})
    for _ in range(120):
        b.chat.append(offer["id"], "x", secrets.token_hex(16), "receiver")
    with pytest.raises(ValueError, match="过快"):
        b.chat.append(offer["id"], "x", secrets.token_hex(16), "receiver")


@pytest.mark.parametrize("text", ["", "  ", None, "x\x00", "中" * 1366])
def test_invalid_text(text):
    with pytest.raises(ValueError):
        message_text(text)


def test_chat_page_size(pair):
    a, b = pair
    offer = a.begin(b.as_peer(), [], chat=True)
    b.chat.decide(offer["id"], True)
    request(a, b, offer, "activate", {})
    for _ in range(12):
        b.chat.append(offer["id"], "x" * 4096, secrets.token_hex(16), "receiver")
    page = request(a, b, offer, "0")
    assert len(page["messages"]) == 4
    assert len(request(a, b, offer, "8")["messages"]) == 4
