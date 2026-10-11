"""Exercise real HTTPS, pairing boundaries and transfer integrity."""

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import quote

import pytest

from lan_drop.server import create_server, is_lan, safe_name


@pytest.fixture
def service(tmp_path):
    server, fingerprint = create_server("127.0.0.1", 0, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(tmp_path / "certificate.pem"))
    yield server, context, fingerprint
    server.stop()
    thread.join(timeout=5)


def request(service, method, path, body=None, token=None, headers=None):
    server, context, _ = service
    connection = http.client.HTTPSConnection(
        "127.0.0.1", server.server_port, context=context, timeout=5
    )
    combined = {"Host": server.authority, "Origin": server.origin}
    if token:
        combined["Cookie"] = token
    combined.update(headers or {})
    connection.request(method, path, body=body, headers=combined)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def pair(service):
    status, headers, _ = request(
        service, "POST", "/api/pair", json.dumps({"code": service[0].state.code}).encode()
    )
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert all(flag in cookie for flag in ["Secure", "HttpOnly", "SameSite=Strict"])
    return cookie.split(";")[0]


def test_private_network_and_names():
    assert all(is_lan(ip) for ip in ["192.168.1.2", "10.2.3.4", "172.16.0.1", "127.0.0.1"])
    assert not any(is_lan(ip) for ip in ["8.8.8.8", "0.0.0.0", "172.32.0.1", "::1"])
    assert safe_name("电路图.pdf") == "电路图.pdf"
    for name in ["../secret", "a\\b", "CON.txt", "NUL", "a:b", "x\n", "x.", "x ", ""]:
        with pytest.raises(ValueError):
            safe_name(name)


def test_auth_and_cross_site_boundaries(service):
    assert request(service, "GET", "/api/files")[0] == 401
    assert request(service, "GET", "/download/anything")[0] == 401
    assert request(service, "GET", "/")[0] == 200
    assert request(service, "GET", "/", headers={"Host": "evil.test"})[0] == 403
    assert (
        request(service, "POST", "/api/pair", b"{}", headers={"Origin": "https://evil.test"})[0]
        == 403
    )
    token = pair(service)
    assert (
        request(
            service,
            "POST",
            "/api/upload",
            b"x",
            token,
            {"Origin": "https://evil.test", "X-Filename": "test.txt"},
        )[0]
        == 403
    )
    assert (
        request(
            service, "POST", "/api/upload", b"x", token, {"X-Filename": quote("../escape.txt")}
        )[0]
        == 400
    )
    assert request(service, "GET", "/api/files", token=token)[0] == 200
    assert request(service, "POST", "/api/logout", b"", token)[0] == 200
    assert request(service, "GET", "/api/files", token=token)[0] == 401


def test_pairing_rate_limit_and_session_binding(service):
    for _ in range(5):
        assert request(service, "POST", "/api/pair", b'{"code":"wrong"}')[0] == 403
    assert request(service, "POST", "/api/pair", b'{"code":"wrong"}')[0] == 429
    state = service[0].state
    status, token = state.pair("192.168.1.22", state.code)
    assert status == 200
    assert state.authenticated(token, "192.168.1.22")
    assert not state.authenticated(token, "192.168.1.23")
    state.sessions[token] = ("192.168.1.22", time.monotonic() - 1)
    assert not state.authenticated(token, "192.168.1.22")


def test_streaming_unicode_duplicate_names_and_hash(service):
    token = pair(service)
    payload = bytes(range(256)) * 9000
    identifiers = []
    for _ in range(2):
        status, _, body = request(
            service, "POST", "/api/upload", payload, token, {"X-Filename": quote("测试.bin")}
        )
        assert status == 201
        data = json.loads(body)
        assert data["sha256"] == hashlib.sha256(payload).hexdigest()
        identifiers.append(data["id"])
    assert identifiers[0] != identifiers[1]
    assert len(service[0].state.files) == 2
    status, headers, body = request(service, "GET", "/download/" + identifiers[0], token=token)
    assert status == 200 and body == payload
    assert headers["X-Content-SHA256"] == hashlib.sha256(body).hexdigest()
    assert "filename*=UTF-8''" in headers["Content-Disposition"]
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert request(service, "GET", "/download/../../private-key.pem", token=token)[0] == 404


def test_size_limit_zero_byte_and_explicit_selection(service, tmp_path):
    server = service[0]
    server.state.max_file = 10
    token = pair(service)
    assert (
        request(service, "POST", "/api/upload", b"a" * 11, token, {"X-Filename": "big.txt"})[0]
        == 400
    )
    assert not server.state.files
    assert (
        request(service, "POST", "/api/upload", b"", token, {"X-Filename": "empty.txt"})[0] == 201
    )
    path = tmp_path / "source.txt"
    path.write_bytes(b"before")
    item = server.state.publish(path)
    path.write_bytes(b"after")
    assert item.path.read_bytes() == b"before"
    assert item.name == "source.txt"
    assert "source.txt" in item.path.name


def test_interrupted_upload_not_published(service):
    server, context, _ = service
    token = pair(service)
    sock = context.wrap_socket(
        socket.create_connection(("127.0.0.1", server.server_port)), server_hostname="127.0.0.1"
    )
    sock.sendall(
        (
            f"POST /api/upload HTTP/1.1\r\nHost: {server.authority}\r\n"
            f"Origin: {server.origin}\r\nCookie: {token}\r\n"
            "X-Filename: partial.txt\r\nContent-Length: 10000\r\n\r\npartial"
        ).encode()
    )
    time.sleep(0.1)
    sock.close()
    for _ in range(30):
        time.sleep(0.05)
        if not list(server.state.received.glob("*.part")):
            break
    assert not list(server.state.received.glob("*.part"))
    assert not server.state.files


def test_tls_fingerprint_matches_certificate(service):
    server, context, fingerprint = service
    with context.wrap_socket(
        socket.create_connection(("127.0.0.1", server.server_port)), server_hostname="127.0.0.1"
    ) as connection:
        assert connection.version() in {"TLSv1.2", "TLSv1.3"}
        digest = hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest().upper()
        assert fingerprint.replace(":", "") == digest
