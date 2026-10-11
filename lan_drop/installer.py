"""User-triggered portable EXE updater, without a shell or administrator rights.

The running image cannot overwrite itself on Windows. A copy of the current
EXE runs this helper from a unique sibling staging directory, waits for the
old image to unlock, then swaps the verified image and restarts it. Data is
never moved. Keep a backup and a JSON result for recovery and diagnosis.
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from .updates import MAX_DOWNLOAD, version_tuple

MAX_EXE = 128 * 1024**2


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_exe(path):
    """Require a Windows x64 PE executable, not arbitrary ZIP contents."""
    with Path(path).open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("更新包中的 EXE 无效")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= min(MAX_EXE, Path(path).stat().st_size - 6):
            raise ValueError("更新包中的 EXE 头无效")
        stream.seek(offset)
        if stream.read(6) != b"PE\0\0\x64\x86":
            raise ValueError("更新包必须是 Windows x64 EXE")


def prepare(archive, release, target, arguments):
    """Recheck the downloaded digest and extract only the fixed executable.

    Stage on the EXE's volume to discover write-permission problems before
    shutting down, and to make replacement atomic. Never extract ZIP paths.
    """
    archive, target = Path(archive).resolve(), Path(target).absolute()
    if target.is_symlink() or not target.is_file():
        raise ValueError("当前 EXE 路径无效")
    if archive.stat().st_size != release.size or not 0 < release.size <= MAX_DOWNLOAD:
        raise ValueError("更新包大小发生变化")
    if digest(archive) != release.digest:
        raise ValueError("更新包校验失败，请重新下载")
    version_tuple(release.version)
    stage = Path(tempfile.mkdtemp(prefix=".lindrop-update-", dir=target.parent))
    try:
        image = stage / "new.exe"
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 8 or names.count("邻传.exe") != 1:
                raise ValueError("更新包缺少唯一的邻传.exe")
            allowed = {"邻传.exe", "README.md", "使用说明.md"}
            if any(name not in allowed for name in names):
                raise ValueError("更新包包含非预期文件")
            entry = bundle.getinfo("邻传.exe")
            if not 64 <= entry.file_size <= MAX_EXE or entry.flag_bits & 1:
                raise ValueError("更新 EXE 大小或格式无效")
            with bundle.open(entry) as source, image.open("xb") as output:
                count = 0
                while block := source.read(256 * 1024):
                    count += len(block)
                    if count > MAX_EXE:
                        raise ValueError("解压文件过大")
                    output.write(block)
        check_exe(image)
        helper = stage / "helper.exe"
        shutil.copyfile(target, helper)
        job = {
            "schema": 1,
            "target": target.name,
            "old_sha256": digest(helper),
            "new_sha256": digest(image),
            "version": release.version,
            "arguments": list(arguments),
            "parent_pid": os.getpid() if getattr(sys, "frozen", False) else None,
        }
        manifest = stage / "job.json"
        manifest.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return manifest
    except Exception:
        # Only this invocation's mkdtemp directory is removed.
        shutil.rmtree(stage)
        raise


def load_job(manifest):
    manifest = Path(manifest).resolve()
    stage = manifest.parent
    if manifest.name != "job.json" or not stage.name.startswith(".lindrop-update-"):
        raise ValueError("更新任务路径无效")
    if manifest.stat().st_size > 16384:
        raise ValueError("更新任务过大")
    job = json.loads(manifest.read_text(encoding="utf-8"))
    name = job.get("target", "")
    if (
        job.get("schema") != 1
        or not isinstance(name, str)
        or not name.lower().endswith(".exe")
        or any(char in name for char in "/\\:\0")
        or name in {".", ".."}
    ):
        raise ValueError("更新目标无效")
    version_tuple(job.get("version"))
    arguments = job.get("arguments")
    if (
        not isinstance(arguments, list)
        or len(arguments) > 32
        or any(not isinstance(arg, str) or len(arg) > 4096 or "\0" in arg for arg in arguments)
    ):
        raise ValueError("重启参数无效")
    target = stage.parent / name
    for path, expected in (
        (stage / "helper.exe", job.get("old_sha256")),
        (stage / "new.exe", job.get("new_sha256")),
        (target, job.get("old_sha256")),
    ):
        if path.is_symlink() or digest(path) != expected:
            raise ValueError("更新文件发生变化，已停止安装")
    check_exe(stage / "new.exe")
    return job, target, stage


def wait_parent(pid, stage, deadline):
    """Wait for the exact old GUI process without terminating any process.

    The PyInstaller bootloader can hold the image a little longer after this
    child exits; the subsequent rename retry covers that remaining image lock.
    """
    if pid is None:
        return
    if os.name != "nt" or type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise ValueError("旧程序进程无效")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        if ctypes.get_last_error() == 87:  # Parent already exited.
            return
        raise OSError("无法等待旧程序退出")
    try:
        while kernel.WaitForSingleObject(handle, 200) == 258:  # WAIT_TIMEOUT
            if (stage / "cancel").exists():
                raise InterruptedError("更新已取消")
            if time.monotonic() >= deadline:
                raise TimeoutError("旧程序尚未退出，更新取消")
    finally:
        kernel.CloseHandle(handle)


def launch(command):
    return subprocess.Popen(
        command,
        cwd=str(Path(command[0]).parent),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def start_helper(manifest):
    """Return only after the helper validated its task; the UI may then quit."""
    stage = Path(manifest).parent
    process = launch([str(stage / "helper.exe"), "--apply-update", str(manifest)])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (stage / "ready").exists():
            return
        if process.poll() is not None:
            raise ValueError("更新助手启动失败，当前版本仍可继续使用")
        time.sleep(0.1)
    (stage / "cancel").touch()
    raise TimeoutError("更新助手启动超时，当前版本仍可继续使用")


def apply(manifest, *, launcher=launch, timeout=60):
    """Helper-only entry: wait for Windows image locks; roll back on swap errors.

    No process is forcibly terminated. A failed process launch restores the old
    EXE. Successful launch is not a health check of all future app features.
    """
    stage = Path(manifest).resolve().parent
    backup, swapped = stage / "previous.exe", False
    try:
        job, target, stage = load_job(manifest)
        (stage / "ready").touch()
        deadline = time.monotonic() + timeout
        wait_parent(job.get("parent_pid"), stage, deadline)
        while True:
            if (stage / "cancel").exists():
                raise InterruptedError("更新已取消")
            try:
                target.rename(backup)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("旧程序尚未退出，更新取消；请关闭所有邻传窗口后重试")
                time.sleep(0.2)
        try:
            if digest(backup) != job["old_sha256"]:
                raise ValueError("原程序已变化，更新取消")
            if digest(stage / "new.exe") != job["new_sha256"]:
                raise ValueError("安装文件校验失败")
            (stage / "new.exe").replace(target)
            swapped = True
            launcher([str(target), *job["arguments"]])
        except Exception:
            backup.replace(target)
            swapped = False
            launcher([str(target), *job["arguments"]])
            raise
        result = {"ok": True, "version": job["version"], "backup": str(backup)}
    except Exception as error:
        result = {"ok": False, "error": str(error), "installed": swapped}
    (stage / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def run_helper(manifest):
    # A copied packaged image may execute this mode; source mode is for unit tests only.
    if not getattr(sys, "frozen", False):
        raise ValueError("自动更新助手仅供便携 EXE 使用")
    if Path(sys.executable).resolve() != Path(manifest).resolve().parent / "helper.exe":
        raise ValueError("请从 App 内发起更新")
    result = apply(manifest)
    if not result["ok"] and os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, result["error"], "邻传更新未完成", 0x10)
