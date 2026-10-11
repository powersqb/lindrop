"""Opt-in GitHub release checks and bounded, checksum-verified ZIP downloads.

LAN networking never calls this module. Downloads are staged; installation
requires a separate explicit click so the user can review the release first.
GitHub's HTTPS digest protects transport integrity, not a compromised account.
"""

import hashlib
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY = "powersqb/lindrop"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_DOWNLOAD = 128 * 1024**2
PACKAGE_NAME = "lindrop-windows-x64.zip"
VERSION = re.compile(r"v?(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\Z")
SHA256 = re.compile(r"sha256:([a-f0-9]{64})\Z")
DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)


def version_tuple(version):
    match = VERSION.fullmatch(version) if isinstance(version, str) else None
    if not match:
        raise ValueError("只支持正式版 x.y.z 版本号")
    return tuple(int(part) for part in match.groups())


def validate_url(url, hosts):
    if not isinstance(url, str):
        raise ValueError("更新地址无效")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("更新地址必须是官方 GitHub HTTPS 地址")
    return url


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        validate_url(new_url, DOWNLOAD_HOSTS)
        return super().redirect_request(request, file, code, message, headers, new_url)


def open_url(url):
    """Use normal HTTPS verification; never disable TLS for network failures."""
    validate_url(url, DOWNLOAD_HOSTS | {"api.github.com"})
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), GitHubRedirect()
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lindrop-update-check",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return opener.open(request, timeout=15)


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    asset_url: str
    filename: str
    digest: str
    size: int
    notes: str


def parse_release(payload):
    if (
        not isinstance(payload, dict)
        or payload.get("draft") is not False
        or payload.get("prerelease") is not False
    ):
        raise ValueError("更新信息不是正式发布版本")
    tag = payload.get("tag_name")
    version_tuple(tag)
    if not tag.startswith("v"):
        raise ValueError("发布标签需要使用 v 前缀")
    page = f"{RELEASES_URL}/tag/{tag}"
    if payload.get("html_url") != page:
        raise ValueError("更新信息来自其他仓库")
    filename = PACKAGE_NAME
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) > 128:
        raise ValueError("发布附件无效")
    candidates = [
        item for item in assets if isinstance(item, dict) and item.get("name") == filename
    ]
    if not candidates:
        filename = f"lindrop-{tag}-windows-x64.zip"  # Read historical releases too.
        candidates = [
            item for item in assets if isinstance(item, dict) and item.get("name") == filename
        ]
    if len(candidates) != 1:
        raise ValueError("此版本尚无 Windows x64 发布包，请打开发布页查看")
    asset = candidates[0]
    digest = asset.get("digest")
    match = SHA256.fullmatch(digest) if isinstance(digest, str) else None
    if not match or asset.get("state") != "uploaded":
        raise ValueError("发布包缺少可信 SHA-256 校验信息，已停止自动下载")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
        raise ValueError("发布包大小无效或超过 128 MiB")
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"
    if asset.get("browser_download_url") != url:
        raise ValueError("下载地址与指定仓库和版本不符")
    notes = payload.get("body")
    return Release(
        tag[1:], page, url, filename, match[1], size, notes[:2000] if isinstance(notes, str) else ""
    )


def check_latest(current, *, opener=open_url):
    try:
        with opener(API_URL) as response:
            payload = response.read(256 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError("仓库尚无正式发布，或仓库需要登录；可打开 GitHub 发布页查看") from None
        if error.code in {403, 429}:
            raise ValueError("GitHub 检查频率受限，请稍后重试") from None
        raise ValueError(f"GitHub 暂时不可用（HTTP {error.code}）") from None
    if len(payload) > 256 * 1024:
        raise ValueError("更新信息过大")
    release = parse_release(json.loads(payload))
    return release, version_tuple(release.version) > version_tuple(current)


def download(release, directory, cancelled, progress, *, opener=open_url):
    """Write an immutable staged ZIP only after both length and hash agree."""
    validate_url(release.asset_url, {"github.com"})
    version_tuple(release.version)
    expected_name = release.filename
    expected_url = (
        f"https://github.com/{REPOSITORY}/releases/download/v{release.version}/{expected_name}"
    )
    if (
        expected_name not in {PACKAGE_NAME, f"lindrop-v{release.version}-windows-x64.zip"}
        or release.asset_url != expected_url
    ):
        raise ValueError("更新包不属于指定仓库或版本")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # The Release object normally comes from parse_release, but bound it here too.
    if (
        Path(release.filename).name != release.filename
        or "/" in release.filename
        or "\\" in release.filename
        or not 0 < release.size <= MAX_DOWNLOAD
        or not re.fullmatch("[a-f0-9]{64}", release.digest)
    ):
        raise ValueError("更新包信息无效")
    target = directory / release.filename
    temporary = directory / (release.filename + "." + secrets.token_hex(16) + ".part")
    count, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 300
    try:
        with opener(release.asset_url) as response, temporary.open("xb") as output:
            while True:
                if cancelled.is_set():
                    raise InterruptedError("下载已取消")
                if time.monotonic() > deadline:
                    raise TimeoutError("更新下载超时")
                block = response.read(min(256 * 1024, release.size - count + 1))
                if not block:
                    break
                count += len(block)
                if count > release.size:
                    raise ValueError("下载长度超出发布信息，已停止下载")
                output.write(block)
                digest.update(block)
                progress(count, release.size)
        if cancelled.is_set():
            raise InterruptedError("下载已取消")
        if count != release.size or digest.hexdigest() != release.digest:
            raise ValueError("更新包校验失败，未保留下载文件")
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
