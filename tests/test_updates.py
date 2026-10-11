"""No live network needed: test update origin, bounds, integrity and cancellation."""

import dataclasses
import hashlib
import io
import json
import threading
import urllib.error

import pytest

from lan_drop.updates import (
    REPOSITORY,
    check_latest,
    download,
    parse_release,
    validate_url,
    version_tuple,
)


def payload(data=b"synthetic zip contents"):
    filename = "lindrop-v0.4.0-windows-x64.zip"
    return {
        "tag_name": "v0.4.0",
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/v0.4.0",
        "body": "测试说明",
        "assets": [
            {
                "name": filename,
                "state": "uploaded",
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v0.4.0/{filename}",
            }
        ],
    }


def test_new_version_not_lexicographic():
    release, newer = check_latest(
        "0.3.0", opener=lambda _: io.BytesIO(json.dumps(payload()).encode())
    )
    assert newer and release.version == "0.4.0"
    assert version_tuple("1.10.0") > version_tuple("1.9.9")
    for current in ("0.4.0", "0.5.0"):
        assert not check_latest(
            current, opener=lambda _: io.BytesIO(json.dumps(payload()).encode())
        )[1]


def test_stable_package_name_download_and_historical_release_support(tmp_path):
    data = b"verified stable-name release"
    value = payload(data)
    asset = value["assets"][0]
    asset["name"] = "lindrop-windows-x64.zip"
    asset["browser_download_url"] = (
        f"https://github.com/{REPOSITORY}/releases/download/v0.4.0/lindrop-windows-x64.zip"
    )
    release = parse_release(value)
    path = download(
        release, tmp_path, threading.Event(), lambda *_: None, opener=lambda _: io.BytesIO(data)
    )
    assert path.name == "lindrop-windows-x64.zip" and path.read_bytes() == data
    assert parse_release(payload()).filename == "lindrop-v0.4.0-windows-x64.zip"


@pytest.mark.parametrize("version", ["v1.0", "0.4.0-beta", "01.0.0", "1.0.0/../x", None])
def test_reject_invalid_versions(version):
    with pytest.raises(ValueError):
        version_tuple(version)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x",
        "https://github.com.attacker.test/x",
        "https://user:password@github.com/x",
        "https://github.com:8443/x",
        "https://example.com/x",
        "https://github.com/x#fragment",
    ],
)
def test_update_url_trust_boundary(url):
    with pytest.raises(ValueError):
        validate_url(url, {"github.com"})


def test_wrong_repo_missing_digest_or_unfinished_asset():
    for key, value in [
        ("digest", None),
        ("state", "new"),
        ("size", True),
        ("browser_download_url", "https://github.com/attacker/repo/file"),
    ]:
        data = payload()
        data["assets"][0][key] = value
        with pytest.raises(ValueError):
            parse_release(data)
    data = payload()
    data["html_url"] = "https://github.com/attacker/repo/releases/tag/v0.4.0"
    with pytest.raises(ValueError):
        parse_release(data)
    for key in ("draft", "prerelease"):
        data = payload()
        data[key] = True
        with pytest.raises(ValueError):
            parse_release(data)


def test_http_failure_and_bounded_metadata():
    def fail(_):
        raise urllib.error.HTTPError("https://api.github.com", 404, "not found", {}, None)

    with pytest.raises(ValueError, match="正式发布"):
        check_latest("0.3.0", opener=fail)
    with pytest.raises(ValueError, match="过大"):
        check_latest("0.3.0", opener=lambda _: io.BytesIO(b"x" * (256 * 1024 + 1)))


def test_download_integrity_and_cancel(tmp_path):
    data = b"test ZIP\n" * 10000
    release = parse_release(payload(data))
    progress = []
    target = download(
        release,
        tmp_path,
        threading.Event(),
        lambda *p: progress.append(p),
        opener=lambda _: io.BytesIO(data),
    )
    assert target.read_bytes() == data
    assert progress[-1] == (len(data), len(data))
    for broken in (data[:-1], b"x" * len(data), data + b"excess"):
        with pytest.raises(ValueError):
            download(
                release,
                tmp_path,
                threading.Event(),
                lambda *_: None,
                opener=lambda _: io.BytesIO(broken),
            )
        assert target.read_bytes() == data  # a failed update preserves the prior staged ZIP
        assert not list(tmp_path.glob("*.part"))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        download(release, tmp_path, cancelled, lambda *_: None, opener=lambda _: io.BytesIO(data))
    assert not list(tmp_path.glob("*.part"))
    with pytest.raises(ValueError):
        download(
            dataclasses.replace(release, filename="../escape.zip"),
            tmp_path,
            threading.Event(),
            lambda *_: None,
        )
