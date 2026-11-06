"""Optional GitHub Release updates; never need repository credentials on clients."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from app.core.cancellation import CancellationToken, cancellation_scope
from app.core.http import http_session
from app.desktop.bootstrap import VERSION, data_directory, resource_root


REPOSITORY = "ooLemon0-0/Data_Auto_Analyzer"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ASSET_NAME = "AutoAnalyzer.exe"


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str
    size: int
    notes: str = ""


def version_tuple(version):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(version))
    if not match:
        raise ValueError("无效的正式版本号")
    return tuple(int(part) for part in match.groups())


def _asset_url(value):
    url = urlsplit(str(value))
    return (url.scheme == "https" and url.netloc == "github.com"
            and url.path.startswith(f"/{REPOSITORY}/releases/download/")
            and not url.username and not url.password)


def _read_small(session, url, token, *, limit=1_048_576, deadline=None):
    with session.get(url, timeout=(2, 3), stream=True) as response:
        response.raise_for_status()
        chunks, count = [], 0
        for chunk in response.iter_content(16384):
            token.check()
            count += len(chunk)
            if count > limit or (deadline is not None and time.monotonic() > deadline):
                raise ValueError("更新检查响应过大或超时")
            chunks.append(chunk)
        return b"".join(chunks)


def check_for_update(token: CancellationToken, current=VERSION) -> Release | None:
    """All network / metadata failures mean continue startup, without a prompt."""
    try:
        with cancellation_scope(token), http_session() as session:
            session.headers.update({"Accept": "application/vnd.github+json",
                                    "User-Agent": f"AutoAnalyzer/{current}",
                                    "X-GitHub-Api-Version": "2026-03-10"})
            deadline = time.monotonic() + 6
            metadata = json.loads(_read_small(session, LATEST_URL, token, deadline=deadline))
            version = str(metadata["tag_name"]).removeprefix("v")
            if metadata.get("draft") or metadata.get("prerelease") or version_tuple(version) <= version_tuple(current):
                return None
            assets = metadata.get("assets", [])
            asset = next(a for a in assets if a.get("name") == ASSET_NAME and a.get("state") == "uploaded")
            url, size = asset["browser_download_url"], int(asset["size"])
            if not _asset_url(url) or size < 2 or size > 2_000_000_000:
                return None
            digest = str(asset.get("digest") or "").removeprefix("sha256:")
            if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                checksum = next(a for a in assets if a.get("name") == "SHA256SUMS.txt")
                if not _asset_url(checksum["browser_download_url"]):
                    return None
                lines = _read_small(session, checksum["browser_download_url"], token, limit=16384, deadline=deadline).decode("utf-8-sig")
                digest = next(line.split()[0] for line in lines.splitlines()
                              if len(line.split()) == 2 and line.split()[1].lstrip("*") == ASSET_NAME)
            if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                return None
            token.check()
            return Release(version, url, digest.lower(), size, str(metadata.get("body") or "")[:2400])
    except Exception:
        return None


def download_update(release: Release, token: CancellationToken, progress, root=None) -> Path:
    version_tuple(release.version)
    if not _asset_url(release.url) or not re.fullmatch(r"[a-f0-9]{64}", release.sha256):
        raise ValueError("更新文件信息无效")
    folder = Path(root or data_directory() / "updates") / release.version
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ASSET_NAME
    partial = folder / (uuid.uuid4().hex + ".part")
    try:
        with cancellation_scope(token), http_session() as session:
            session.headers["User-Agent"] = f"AutoAnalyzer/{VERSION}"
            with session.get(release.url, stream=True, timeout=(3, 5)) as response:
                response.raise_for_status()
                digest, count, header = hashlib.sha256(), 0, b""
                with partial.open("wb") as output:
                    for chunk in response.iter_content(262144):
                        token.check()
                        if not chunk:
                            continue
                        count += len(chunk)
                        if count > release.size:
                            raise ValueError("更新文件大小不符，请稍后重试")
                        header = (header + chunk)[:2]
                        digest.update(chunk)
                        output.write(chunk)
                        progress(count, release.size)
                token.check()
                if count != release.size or header != b"MZ" or digest.hexdigest() != release.sha256:
                    raise ValueError("更新文件校验未通过，请稍后重试")
                partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def launch_installer(downloaded: Path, release: Release, *, executable=None):
    if sys.platform != "win32":
        raise RuntimeError("自动安装仅适用于 Windows")
    target = Path(executable or sys.executable).resolve()
    downloaded = downloaded.resolve()
    if target == downloaded or not target.is_file():
        raise ValueError("当前程序路径无效")
    # Fail in the app, before exit, when a protected install folder is unwritable.
    probe = target.with_name(".autoanalyzer-update-" + uuid.uuid4().hex + ".tmp")
    try:
        probe.write_bytes(b"")
    finally:
        probe.unlink(missing_ok=True)
    script = downloaded.parent / "install-update.ps1"
    script.write_bytes((resource_root() / "app/desktop/assets/install-update.ps1").read_bytes())
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(script), "-SourcePath", str(downloaded), "-TargetPath", str(target),
        "-AppProcessId", str(os.getpid()), "-ExpectedHash", release.sha256],
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
