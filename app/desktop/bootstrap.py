from __future__ import annotations

import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys


APP_NAME = "AutoAnalyzer"
VERSION = "1.6.11"
RELEASE_NOTES = (
    "• 数据采集在后台运行，采集期间可以继续翻图和审核。用「采集进度」查看或停止任务。\n\n"
    "• 修复数据采集及其他日期下拉日历中部分日期显示为空白的问题。\n\n"
    "• 修复旋转后的图片在缩放时跳回其他位置。\n\n"
    "• 新增「数据采集」，按点位和日期范围随机抽取原图，并批量保存到本地。\n\n"
    "• EasyConnect 改为按需启动。拉取镔鑫数据遇到内网不通时，或手动点击「检查 / 启动 EasyConnect」时，才会尝试启动。\n\n"
    "• 主界面新增「更新公告」。升级后，前三次启动会自动显示本版公告。\n\n"
    "• 新增越南铸坯号识别。先配置好 GlobalProtect 和网页中转，再拉取数据。\n\n"
    "• 越南历史记录和原图下载支持断线重试。原图校验通过后写入本地缓存。\n\n"
    "• 越南审核统计同步到指定轻推文档，使用现场的六列表头。"
)


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])).resolve()


def data_directory() -> Path:
    configured = os.environ.get("AUTOANALYZER_DATA_DIR")
    root = Path(configured) if configured else Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def bundled_browser() -> Path | None:
    from app.desktop.startup import report_startup
    report_startup("检查浏览器组件")
    candidate = resource_root() / "desktop_assets/chromium/chrome.exe"
    if not candidate.exists():
        if getattr(sys, "frozen", False):
            raise RuntimeError("程序内置浏览器缺失，请重新下载完整的 exe")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            installed = Path(playwright.chromium.executable_path)
        if installed.exists():
            return installed
        return None
    # The launcher keeps the entire runtime in AppData. Its browser no longer
    # lives in a disposable one-file directory, so a second copy is unnecessary.
    if getattr(sys, "frozen", False) and os.environ.get("AUTOANALYZER_LAUNCHER_EXE"):
        return candidate
    # A one-file executable extracts into a temporary directory. Keep the browser
    # in AppData so a saved login window survives application updates and exit.
    manifest_path = resource_root() / "desktop_assets/build_info.json"
    revision = json.loads(manifest_path.read_text(encoding="utf-8")).get("browser_revision", "bundled")
    browser_root = data_directory() / "browser-runtime"
    target = browser_root / str(revision)
    if not target.resolve().is_relative_to(browser_root.resolve()):
        raise RuntimeError("内置浏览器版本路径无效")
    marker = target / ".complete"
    if not marker.exists() or not (target / "chrome.exe").exists():
        target.mkdir(parents=True, exist_ok=True)
        files = [path for path in candidate.parent.rglob("*") if path.is_file()]
        for index, path in enumerate(files, 1):
            destination = target / path.relative_to(candidate.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            report_startup("安装浏览器组件", index, len(files))
        marker.write_text(str(revision), encoding="utf-8")
    return target / "chrome.exe"


def relocate_config(config: dict, root: Path, browser: Path | None) -> dict:
    result = copy.deepcopy(config)
    root = root.resolve()
    result["storage"] = {"db_path": str(root / "review.sqlite3"), "data_root": str(root / "data")}
    for project in result["projects"]:
        project["diagnostics"] = {"enabled": False}
        source = project["source"]
        source["prefetch_sample_images"] = True
        source_browser = source.setdefault("browser", {})
        source_browser.update(user_data_dir=str(root / "browser" / project["id"]),
                              download_dir=str(root / "downloads" / project["id"]), headless=True)
        sink_browser = project.setdefault("sink", {}).setdefault("browser", {})
        project['sink'].setdefault('format', {}).setdefault('include_other', True)
        port, profile = 19423, "qingtui"
        if sink_browser.get("dedicated_port"):
            port, profile = int(sink_browser["dedicated_port"]), project["id"]
            sink_browser.pop("remote_debugging_url", None)
        sink_browser.update(user_data_dir=str(root / "browser" / profile), remote_debugging_port=port,
                            allow_interactive_login=True, interactive_login_timeout_seconds=300)
        if browser is not None:
            for browser_config in (source_browser, sink_browser):
                browser_config["executable_path"] = str(browser)
                browser_config.pop("channel", None)
        project.setdefault("cache", {}).update(path=str(root / "review.sqlite3"),
                                                 result_export_dir=str(root / "results" / project["id"]))
    for connection in result.get("remote_connections", []):
        socks = connection.get("socks_over_rdp", {})
        socks["source_dir"] = str(resource_root() / "dependencies")
    return result


def load_desktop_config() -> dict:
    from app.desktop.startup import report_startup
    report_startup("读取点位配置")
    path = resource_root() / "desktop_assets/config.json"
    if not path.exists() and not getattr(sys, "frozen", False):
        path = resource_root() / "config/config.json"
    if not path.exists():
        raise RuntimeError("程序内置配置缺失，请重新下载完整的桌面程序")
    root = data_directory()
    return relocate_config(desktop_config(path, root), root, bundled_browser())


def desktop_config(bundled_path: Path, root: Path) -> dict:
    local = root / "config.json"
    if local.is_file():
        return json.loads(local.read_text(encoding="utf-8-sig"))

    bundled = json.loads(bundled_path.read_text(encoding="utf-8"))
    metadata = bundled_path.with_name("build_info.json")
    if not metadata.is_file() or json.loads(metadata.read_text(encoding="utf-8")).get("configuration") != "example":
        return bundled

    # Reuse settings from the latest ready-to-run private package in this Windows profile.
    # Keep new sample projects from the public build if the older version did not have them.
    previous = sorted((root / "app-runtime").glob("*/.complete"),
                      key=lambda marker: marker.stat().st_mtime_ns, reverse=True)
    for marker in previous:
        assets = marker.parent / "_internal/desktop_assets"
        config, info = assets / "config.json", assets / "build_info.json"
        try:
            if config.resolve() == bundled_path.resolve():
                continue
            if info.is_file() and json.loads(info.read_text(encoding="utf-8")).get("configuration") == "example":
                continue
            old = json.loads(config.read_text(encoding="utf-8-sig"))
            from app.core.config import AppConfig
            AppConfig.model_validate(old)
            if not old.get("projects"):
                continue
        except (OSError, ValueError):
            continue

        projects = {project["id"]: project for project in bundled["projects"]}
        projects.update((project["id"], project) for project in old["projects"])
        return {**bundled, **old, "projects": list(projects.values())}
    return bundled


def initialize() -> Path:
    os.environ["AUTOANALYZER_DESKTOP"] = "1"
    root = data_directory()
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "desktop.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return root
