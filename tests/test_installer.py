"""Verified archive boundaries, atomic replacement and recoverable failures."""

import hashlib
import json
import struct
import zipfile

import pytest

from lan_drop.installer import apply, prepare
from lan_drop.updates import Release


def pe_image(contents):
    header = bytearray(64)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    return bytes(header) + b"PE\0\0\x64\x86" + contents


def package(tmp_path, entries=None):
    target = tmp_path / "邻传.exe"
    target.write_bytes(pe_image(b"old image"))
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in (entries or {"邻传.exe": pe_image(b"new image")}).items():
            bundle.writestr(name, data)
    release = Release(
        "0.6.0",
        "",
        "",
        archive.name,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        archive.stat().st_size,
        "",
    )
    return target, archive, release


def test_installer_replaces_exact_exe_keeps_data_and_backup(tmp_path):
    target, archive, release = package(tmp_path)
    data = tmp_path / "邻传数据"
    data.mkdir()
    identity = data / "device-id"
    identity.write_text("my persistent identity")
    old = target.read_bytes()
    arguments = ["--data-dir", str(data), "--name", '空格 & "字符']
    manifest = prepare(archive, release, target, arguments)
    launched = []
    result = apply(manifest, launcher=launched.append)
    assert result["ok"]
    assert target.read_bytes() == pe_image(b"new image")
    assert (manifest.parent / "previous.exe").read_bytes() == old
    assert identity.read_text() == "my persistent identity"
    assert launched == [[str(target), *arguments]]


def test_launch_failure_restores_original_and_restarts_it(tmp_path):
    target, archive, release = package(tmp_path)
    original = target.read_bytes()
    manifest = prepare(archive, release, target, [])
    calls = []

    def launch(command):
        calls.append(command)
        if len(calls) == 1:
            raise OSError("cannot start new image")

    result = apply(manifest, launcher=launch)
    assert not result["ok"] and not result["installed"]
    assert target.read_bytes() == original
    assert len(calls) == 2


@pytest.mark.parametrize(
    "entries",
    [
        {"../邻传.exe": pe_image(b"bad path")},
        {"邻传.exe": b"not an executable"},
        {"邻传.exe": pe_image(b"new"), "evil.exe": pe_image(b"other")},
    ],
)
def test_bad_archives_never_replace_target(tmp_path, entries):
    target, archive, release = package(tmp_path, entries)
    original = target.read_bytes()
    with pytest.raises(ValueError):
        prepare(archive, release, target, [])
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".lindrop-update-*"))


@pytest.mark.parametrize("tamper", ["new.exe", "helper.exe", "target", "job.json", "cancel"])
def test_changed_or_cancelled_task_does_not_install(tmp_path, tamper):
    target, archive, release = package(tmp_path)
    manifest = prepare(archive, release, target, [])
    if tamper == "job.json":
        job = json.loads(manifest.read_text("utf-8"))
        job["target"] = "../outside.exe"
        manifest.write_text(json.dumps(job), encoding="utf-8")
    elif tamper == "cancel":
        (manifest.parent / "cancel").touch()
    else:
        path = target if tamper == "target" else manifest.parent / tamper
        path.write_bytes(b"tampered")
    before = target.read_bytes()
    result = apply(manifest, launcher=lambda _: pytest.fail("must not launch"))
    assert not result["ok"]
    assert target.read_bytes() == before


def test_rechecks_archive_digest_before_extracting(tmp_path):
    target, archive, release = package(tmp_path)
    content = archive.read_bytes()
    archive.write_bytes(b"x" + content[1:])
    with pytest.raises(ValueError, match="校验"):
        prepare(archive, release, target, [])


def test_locked_target_times_out_without_force_closing(tmp_path, monkeypatch):
    from pathlib import Path

    target, archive, release = package(tmp_path)
    original = target.read_bytes()
    manifest = prepare(archive, release, target, [])
    rename = Path.rename

    def locked(path, destination):
        if path == target:
            raise PermissionError("running executable")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", locked)
    result = apply(manifest, launcher=lambda _: pytest.fail("must not launch"), timeout=0)
    assert not result["ok"] and "尚未退出" in result["error"]
    assert target.read_bytes() == original
