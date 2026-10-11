"""Real two-node desktop transfers and app-specific discovery boundaries."""

import dataclasses
import hashlib
import json
import ssl
import threading
import time

import pytest

from lan_drop.discovery import Discovery, parse_beacon
from lan_drop.peer import (
    PeerNode,
    PinnedConnection,
    comparison_code,
    describe_files,
    peer_request,
    validate_files,
)
from lan_drop.server import local_addresses


@pytest.fixture
def nodes(tmp_path):
    a = PeerNode("127.0.0.1", 0, tmp_path / "a", "测试设备 A", discover=False)
    b = PeerNode("127.0.0.1", 0, tmp_path / "b", "测试设备 B", discover=False)
    yield a, b
    a.close()
    b.close()


def prepare(sender, receiver, paths):
    files = describe_files(paths, threading.Event(), lambda *_: None)
    offer = sender.begin(receiver.as_peer(), files)
    incoming = receiver.protocol.offers[offer["id"]]
    assert offer["code"] == incoming.code
    assert incoming.sender.id == sender.id
    return offer, files


def upload_raw(sender, receiver, offer, index, data, token):
    connection = PinnedConnection(receiver.as_peer(), sender.host)
    connection.request(
        "POST",
        f"/peer/upload/{offer['id']}/{index}",
        data,
        {"Origin": receiver.server.origin, "X-Transfer-Token": token},
    )
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


@pytest.mark.parametrize("reverse", [False, True])
def test_bidirectional_batch_transfer(nodes, tmp_path, reverse):
    sender, receiver = reversed(nodes) if reverse else nodes
    directory1, directory2 = tmp_path / "one", tmp_path / "two"
    directory1.mkdir()
    directory2.mkdir()
    file1, file2, empty = directory1 / "你好.bin", directory2 / "你好.bin", directory1 / "空.txt"
    file1.write_bytes(bytes(range(256)) * 9000)
    file2.write_bytes(b"same name, different contents")
    empty.write_bytes(b"")
    paths = [file1, file2, empty]
    offer, files = prepare(sender, receiver, paths)
    receiver.protocol.decide(offer["id"], True)
    token = sender.wait_accepted(receiver.as_peer(), offer, threading.Event())
    for index, (path, item) in enumerate(zip(paths, files, strict=True)):
        sender.send_file(
            receiver.as_peer(), offer, token, index, path, item, threading.Event(), lambda *_: None
        )
    saved = list(receiver.received.iterdir())
    assert len(saved) == 3
    assert {p.read_bytes() for p in saved} == {p.read_bytes() for p in paths}
    assert receiver.protocol.offers[offer["id"]].status == "complete"
    assert not sender.server.state.files and not receiver.server.state.files
    assert upload_raw(sender, receiver, offer, 1, file2.read_bytes(), token)[0] == 400


def test_receive_permission_and_secret_boundaries(nodes, tmp_path):
    a, b = nodes
    source = tmp_path / "private.txt"
    source.write_bytes(b"never sent without acceptance")
    offer, _ = prepare(a, b, [source])
    assert not list(b.received.iterdir())
    assert upload_raw(a, b, offer, 0, source.read_bytes(), "bad-token")[0] == 400
    with pytest.raises(ValueError, match="未授权"):
        peer_request(
            b.as_peer(), "GET", "/peer/offers/" + offer["id"], secret="wrong", source=a.host
        )
    b.protocol.decide(offer["id"], False)
    with pytest.raises(ValueError, match="拒绝"):
        a.wait_accepted(b.as_peer(), offer, threading.Event())
    assert not list(b.received.iterdir())


def test_pin_mismatch_blocks_requests(nodes, tmp_path):
    a, b = nodes
    impostor = dataclasses.replace(b.as_peer(), fingerprint="0" * 64)
    with pytest.raises(ssl.SSLError, match="证书"):
        peer_request(impostor, "GET", "/peer/hello", source=a.host)
    assert not b.protocol.offers
    actual = a.manual_peer(b.host, b.port)
    assert actual.id == b.id and actual.fingerprint == b.fingerprint


def test_hash_failure_and_cancel_remove_partial_files(nodes, tmp_path):
    a, b = nodes
    source = tmp_path / "integrity.txt"
    source.write_bytes(b"correct data")
    offer, _ = prepare(a, b, [source])
    b.protocol.decide(offer["id"], True)
    token = a.wait_accepted(b.as_peer(), offer, threading.Event())
    status, result = upload_raw(a, b, offer, 0, b"wronggg data", token)
    assert status == 400 and "校验" in result["error"]
    assert not list(b.received.iterdir())
    assert b.protocol.offers[offer["id"]].status == "cancelled"
    offer, _ = prepare(a, b, [source])
    a.cancel(b.as_peer(), offer)
    assert b.protocol.offers[offer["id"]].status == "cancelled"
    assert not list(b.received.iterdir())


def test_interrupted_desktop_transfer(nodes, tmp_path):
    a, b = nodes
    source = tmp_path / "partial.bin"
    source.write_bytes(b"X" * 10000)
    offer, _ = prepare(a, b, [source])
    b.protocol.decide(offer["id"], True)
    token = a.wait_accepted(b.as_peer(), offer, threading.Event())
    connection = PinnedConnection(b.as_peer(), a.host)
    connection.connect()
    connection.sock.sendall(
        (
            f"POST /peer/upload/{offer['id']}/0 HTTP/1.1\r\nHost: {b.server.authority}\r\n"
            f"Origin: {b.server.origin}\r\nX-Transfer-Token: {token}\r\n"
            "Content-Length: 10000\r\n\r\nshort"
        ).encode()
    )
    time.sleep(0.1)
    connection.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and b.protocol.offers[offer["id"]].status == "accepted":
        time.sleep(0.05)
    assert b.protocol.offers[offer["id"]].status == "cancelled"
    assert not list(b.received.iterdir())


def test_metadata_validation_and_code_binding():
    good = [{"name": "test.txt", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}]
    assert validate_files(good) == good
    for change in [
        {"name": "../escape"},
        {"size": -1},
        {"size": True},
        {"size": 2**32},
        {"sha256": "fake"},
    ]:
        with pytest.raises(ValueError):
            validate_files([{**good[0], **change}])
    with pytest.raises(ValueError):
        validate_files(good * 51)
    values = ["a" * 64, "b" * 64, "c" * 64, "d" * 32]
    original = comparison_code(*values)
    for i in range(4):
        changed = list(values)
        changed[i] = "f" * len(values[i])
        assert comparison_code(*changed) != original


def test_discovery_only_accepts_app_beacons_and_expires():
    identity = {"id": "a" * 32, "name": "邻传设备", "port": 8765, "fingerprint": "b" * 64}
    beacon = {"app": "lindrop", "protocol": 2, **identity}
    parsed = parse_beacon(json.dumps(beacon).encode(), "192.168.1.5")
    assert parsed.name == "邻传设备"
    for packet, ip in [
        (b"{}", "192.168.1.5"),
        (b"{}", "8.8.8.8"),
        (json.dumps({**beacon, "app": "other"}).encode(), "192.168.1.5"),
        (b"x" * 1501, "192.168.1.5"),
    ]:
        with pytest.raises(ValueError):
            parse_beacon(packet, ip)
    discovery = Discovery.__new__(Discovery)
    discovery.identity = lambda: {**identity, "id": "f" * 32}
    discovery.peers = {}
    discovery.lock = threading.Lock()
    discovery.remember(parsed)
    assert len(discovery.snapshot()) == 1
    discovery.peers[parsed.id] = dataclasses.replace(parsed, seen=time.monotonic() - 10)
    assert not discovery.snapshot()


def test_real_multicast_app_discovery(tmp_path):
    addresses = local_addresses()
    if not addresses:
        pytest.skip("No LAN adapter available for multicast integration test")
    host = addresses[0]
    a = PeerNode(host, 0, tmp_path / "discover-a", "发现测试 A")
    b = PeerNode(host, 0, tmp_path / "discover-b", "发现测试 B")
    try:
        assert a.discovery and b.discovery
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            a_peers, b_peers = a.discovery.snapshot(), b.discovery.snapshot()
            if b.id in {peer.id for peer in a_peers} and a.id in {peer.id for peer in b_peers}:
                break
            time.sleep(0.1)
        assert b.id in {peer.id for peer in a.discovery.snapshot()}
        assert a.id in {peer.id for peer in b.discovery.snapshot()}
        assert a.id not in {peer.id for peer in a.discovery.snapshot()}
        found = next(peer for peer in a.discovery.snapshot() if peer.id == b.id)
        assert peer_request(found, "GET", "/peer/hello", source=host)["id"] == b.id
    finally:
        a.close()
        b.close()
