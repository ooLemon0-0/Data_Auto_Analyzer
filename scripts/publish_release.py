"""Publish an authorized local Windows build as GitHub Release assets.

The Git credential manager supplies a token in memory; no credentials are
written into source, release notes, command arguments, or client binaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.desktop.bootstrap import VERSION
from app.desktop.updates import REPOSITORY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Pushed source commit to release")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "scripts/check_source_secrets.py"), "--staged"], cwd=ROOT, check=True)
    commit = subprocess.check_output(["git", "rev-parse", args.commit], cwd=ROOT, text=True).strip()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        credential = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, check=True, timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        fields = dict(line.split("=", 1) for line in credential.stdout.splitlines() if "=" in line)
        token = fields.get("password")
    if not token:
        raise SystemExit("No GitHub publishing credential available")
    api = f"https://api.github.com/repos/{REPOSITORY}"
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2026-03-10"})
    tag = "v" + VERSION
    lookup = session.get(api + "/releases/tags/" + tag, timeout=20)
    if lookup.status_code == 404:
        response = session.post(api + "/releases", json={"tag_name": tag, "target_commitish": commit,
            "name": f"数据抽样审核 {tag} · Windows", "draft": True, "prerelease": False,
            "body": "Windows 10/11 64 位便携版，下载 AutoAnalyzer.exe 即可运行。\n\n"
                    "- 启动时检查 GitHub 更新；网络不可用直接启动，可随时跳过检查或下载。\n"
                    "- 更新文件校验大小和 SHA-256，自动替换并重启，保留本地数据。\n"
                    "- 支持多点位并发拉取、清晰的分阶段进度、停止任务和中断退出后继续。\n"
                    "- 二期点位优先生成带端面框的复核图；审核完成可同步轻推。\n\n"
                    "便携版内置现场配置及独立浏览器运行环境，面向已授权的现场使用人员。"}, timeout=20)
        response.raise_for_status()
        release = response.json()
    else:
        lookup.raise_for_status()
        release = lookup.json()
        if not release.get("draft"):
            raise SystemExit("Release is already published; use a new version instead of replacing assets")
    upload = release["upload_url"].split("{", 1)[0]
    existing = {asset["name"]: asset for asset in release.get("assets", [])}
    for local_name, asset_name, content_type in [
        ("数据抽样审核.exe", "AutoAnalyzer.exe", "application/octet-stream"),
        ("SHA256SUMS.txt", "SHA256SUMS.txt", "text/plain"),
        ("使用说明.txt", "README-Windows.txt", "text/plain")]:
        local = ROOT / "release" / local_name
        digest = hashlib.file_digest(local.open("rb"), "sha256").hexdigest()
        if asset_name in existing:
            asset = existing[asset_name]
            if asset.get("digest") == "sha256:" + digest and asset.get("size") == local.stat().st_size:
                print(json.dumps({"asset": asset_name, "already_uploaded": True}))
                continue
            raise SystemExit(f"Draft asset {asset_name} differs; inspect the draft before retrying")
        with local.open("rb") as binary:
            response = session.post(upload, params={"name": asset_name}, data=binary,
                headers={"Content-Type": content_type}, timeout=(15, 240))
        response.raise_for_status()
        asset = response.json()
        if asset.get("size") != local.stat().st_size or asset.get("digest") not in (None, "sha256:" + digest):
            raise RuntimeError("Uploaded asset did not match the local build")
        print(json.dumps({"asset": asset_name, "bytes": asset["size"], "sha256": digest}), flush=True)
    response = session.patch(api + "/releases/" + str(release["id"]), json={"draft": False, "make_latest": "true"}, timeout=20)
    response.raise_for_status()
    print(json.dumps({"published": response.json()["html_url"]}), flush=True)


if __name__ == "__main__":
    main()
