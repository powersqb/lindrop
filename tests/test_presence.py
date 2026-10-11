"""First discovery over pinned TLS without multicast, manual input, or auto trust."""

import dataclasses
import json
import ssl
import time

import pytest

from lan_drop.discovery import parse_beacon
from lan_drop.peer import PeerNode, peer_request


def test_rewritten_udp_source_uses_validated_lan_listener_hint():
    payload = {
        "app": "lindrop",
        "protocol": 2,
        "id": "a" * 32,
        "name": "Phone",
        "fingerprint": "b" * 64,
        "port": 8765,
        "host": "192.168.1.6",
        "presence": True,
    }
    peer = parse_beacon(json.dumps(payload).encode(), "192.168.1.8")
    assert peer.host == "192.168.1.6" and peer.presence
    payload["host"] = "8.8.8.8"
    with pytest.raises(ValueError):
        parse_beacon(json.dumps(payload).encode(), "192.168.1.8")


def test_first_presence_reply_discovers_without_trusting(tmp_path):
    a = PeerNode("127.0.0.1", 0, tmp_path / "a", "PC", discover=False)
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", "Phone", discover=False)
    try:
        a.presence.submit(b.as_peer())
        end = time.monotonic() + 5
        while not b.presence.snapshot() and time.monotonic() < end:
            time.sleep(0.05)
        discovered = b.presence.snapshot()
        assert len(discovered) == 1 and discovered[0].id == a.id
        assert discovered[0].host == a.host and discovered[0].port == a.port
        assert not a.trust.peers and not b.trust.peers
        assert not a.chat.channels and not b.chat.channels
        assert not a.protocol.offers and not b.protocol.offers
        # Discovery may not replace the certificate of a previously verified ID.
        a.trust.remember(b.as_peer())
        forged = dataclasses.replace(b.as_peer(), fingerprint="0" * 64)
        with pytest.raises(ssl.SSLError):
            peer_request(forged, "POST", "/peer/presence", a.identity(), source=a.host)
        b.trust.remember(a.as_peer())
        with b.presence.lock:
            b.presence.incoming.clear()
        wrong_sender = {"app": "lindrop", "protocol": 2, **a.identity(), "fingerprint": "0" * 64}
        with pytest.raises(ValueError, match="设备证书已变化"):
            peer_request(b.as_peer(), "POST", "/peer/presence", wrong_sender, source=a.host)
        assert b.trust.check(a.as_peer())
    finally:
        a.close()
        b.close()
