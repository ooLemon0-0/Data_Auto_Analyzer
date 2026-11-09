"""Build the self-contained Windows executable using the local, authorized config."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.desktop.bootstrap import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config/config.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--browser-executable", type=Path, help="Reuse an existing Chromium executable when building offline")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows exe 必须在 Windows 上构建")
    from PIL import Image, ImageDraw
    import PySide6
    from playwright.sync_api import sync_playwright

    stage = ROOT / "build/desktop_assets"
    stage.mkdir(parents=True, exist_ok=True)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    # The document's existing first-phase tab was renamed on site.
    for project in config["projects"]:
        if project["id"] == "binxin_billet":
            project["sink"]["selectors"]["sheet_name"] = "1期-73"
    (stage / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.browser_executable:
        browser = args.browser_executable.resolve()
    else:
        with sync_playwright() as playwright:
            browser = Path(playwright.chromium.executable_path)
    if not browser.exists():
        raise SystemExit("先执行 python -m playwright install chromium --no-shell 安装打包所需的浏览器")
    manifest = {"version": VERSION, "browser_revision": browser.parent.parent.name,
                "built_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")}
    (stage / "build_info.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    icon = Image.new("RGBA", (256, 256), "#16345d")
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((0, 0, 255, 255), radius=55, fill="#6658E8")
    for x, top in ((48, 140), (100, 100), (152, 60)):
        draw.rounded_rectangle((x, top, x + 32, 206), radius=6, fill="#e5f0ff")
    draw.line(((128, 105), (160, 133), (219, 55)), fill="#41d69b", width=17)
    icon.save(stage / "app.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    spec = ROOT / "build/AutoAnalyzer.spec"
    contents = f'''# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules
playwright_data, playwright_binaries, playwright_hidden = collect_all('playwright')
datas = playwright_data + [
    ({str(stage / 'config.json')!r}, 'desktop_assets'),
    ({str(stage / 'build_info.json')!r}, 'desktop_assets'),
    ({str(ROOT / 'app/desktop/assets')!r}, 'app/desktop/assets'),
    ({str(browser.parent)!r}, 'desktop_assets/chromium'),
    ({str(ROOT / 'dependencies')!r}, 'dependencies'),
]
a = Analysis([{str(ROOT / 'desktop.py')!r}], pathex=[{str(ROOT)!r}], binaries=playwright_binaries,
    datas=datas, hiddenimports=playwright_hidden + collect_submodules('app', filter=lambda name: name != 'app.api'),
    excludes=['pytest', 'numpy', 'pandas', 'matplotlib', 'scipy', 'gradio', 'fastapi', 'uvicorn', 'starlette', 'tkinter', 'PIL.ImageTk', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtNetwork', 'PySide6.QtMultimedia'])
# Python loads the runtime from the archive root before Qt is imported. Use
# Qt's newer, backwards-compatible runtime there too; an older Python copy
# otherwise shadows Qt's DLL and produces "procedure not found" on clean PCs.
qt_dir = Path({str(Path(PySide6.__file__).parent)!r})
runtime_names = {{'vcruntime140.dll', 'vcruntime140_1.dll'}}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in runtime_names]
for name in sorted(runtime_names):
    a.binaries.append((name.upper().replace('.DLL', '.dll'), str(qt_dir / name), 'BINARY'))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='AutoAnalyzerCore',
    debug=False, strip=False, upx=False, console=False, icon={str(stage / 'app.ico')!r})
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='AutoAnalyzerRuntime')
'''
    spec.write_text(contents, encoding="utf-8")
    env = os.environ.copy()
    # Do not allow another installed Python's packages to contaminate the bundle.
    env.pop("PYTHONPATH", None)
    env.pop("AUTOANALYZER_DESKTOP", None)
    # PyInstaller inspects the user site path even under -I. Keep that probe
    # inside the build directory, separate from unrelated user installations.
    env["PYTHONUSERBASE"] = str(ROOT / "build/python-user")
    env["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build/tool-cache")
    # Do not collect unrelated DLLs from developer tools on PATH (e.g. Poppler's
    # versioned ICU is incompatible with the Windows ICU API Qt imports).
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    env["PATH"] = os.pathsep.join(map(str, [Path(sys.executable).parent,
        Path(sys.base_prefix), windows / "System32", windows]))
    subprocess.run([sys.executable, "-I", "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build/pyinstaller"), str(spec)],
                   cwd=ROOT, env=env, check=True)
    release = args.output_dir.resolve()
    release.mkdir(parents=True, exist_ok=True)
    target = release / "数据抽样审核.exe"
    from scripts.windows_bundle import build_launcher
    launcher = build_launcher(ROOT / "dist/AutoAnalyzerRuntime", target, stage / "app.ico", stage=ROOT / "build/launcher")
    digest = hashlib.file_digest(target.open("rb"), "sha256").hexdigest()
    update_manifest = {'version': VERSION, 'url': f'https://github.com/ooLemon0-0/Data_Auto_Analyzer/releases/download/v{VERSION}/AutoAnalyzer.exe',
        'sha256': digest, 'size': target.stat().st_size,
        'notes': '审核完成后可选择自动同步轻推，或生成每日统计表并复制到 WPS / 轻推。'}
    (release / 'update.json').write_text(json.dumps(update_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (release / "SHA256SUMS.txt").write_text(f"{digest}  AutoAnalyzer.exe\n{digest}  {target.name}\n", encoding="utf-8")
    (release / "使用说明.txt").write_text(
        f"数据抽样审核 桌面版 {VERSION}（Windows 10/11 64 位）\n\n"
        "1. 双击“数据抽样审核.exe”，无需安装 Python 或浏览器，无需修改配置文件。\n"
        "   先显示加载窗口；运行文件只在首次使用或升级后安装，之后从本机缓存启动。\n"
        "   组件从 EXE 本地解压，无需联网下载；重复打开同一版本会复用已解压文件。\n"
        "   加载窗口显示运行文件校验、组件安装、配置读取和本地任务恢复进度，可随时退出。\n"
        "   启动时必须检查更新；发现新版后自动下载、校验并重启，完成前不能进入旧版。\n"
        "   仅更新服务无法连接时提供“离线进入”；校验、权限或安装错误须重试或退出。\n"
        "   主界面“缓存空间”可按采集日期查看占用、勾选多个日期清理图片；审核记录保留。\n"
        "   正在拉取的日期需先停止任务；清理后的任务可继续拉取图片，沿用原审核结果。\n"
        "   图片缓存达到 10 GB 后，后台从最旧日期逐天清理至低于阈值；正在下载或审核的日期跳过。\n"
        "   自动清理不弹窗，不影响审核和退出；审核记录、统计及登录状态保留。\n"
        "   本地任务支持分类和搜索；“筛选 / 排序”可组合日期范围、审核状态及排序。\n"
        "2. 请先安装企业 EasyConnect 客户端。程序会检查并自动启动；VPN 登录或动态验证需本人完成。\n"
        "3. 点击“拉取数据 · 选择点位”，在弹窗里勾选多个点位、日期和每点有效样本数，再开始拉取。\n"
        "   不同站点最多 4 个项目同时准备；镔鑫图片每点 4 路并发，全局最多 8 张同时下载。\n"
        "4. 任一点位就绪后即可先审核，其余任务继续后台下载。点击左侧任务切换审核点位。\n"
        "   进度窗口可收起或关闭，不会停止任务；点击主界面“下载进度”随时重新查看。\n"
        "   进度页区分记录目录、样本目标和复核图缓存张数，显示当前阶段、排队原因及等待时间。\n"
        "   可单独停止某个点位，或停止全部后台任务；停止后点“继续拉取”复用已完成图片。\n"
        "   点击主窗口关闭，在提示中选“停止并退出”即可中断并退出，不必等整批结束。\n"
        "   未完成任务会保留点位、日期和样本数；下次打开可继续。已保存审核结果不丢失。\n"
        "5. 按 1 正确、2 错误、3 模糊、4 其他；模糊和其他单独记录，不计正确率并自动补抽。\n"
        "   鼠标移到图片上，右上角会显示“保存原图”；确认后保存到系统“下载”文件夹。\n"
        "   只复制现场完整原始文件，不含端面框、局部裁剪和界面旋转；原图缺失会明确提示。\n"
        "   镔鑫二期 172.30.37.* 点位优先显示带端面框的复核图。没有有效框坐标时会明确标注原图。\n"
        "   旧缓存重新拉取时会补齐坐标并生成复核图，复用已有原图、保留审核结果。\n"
        "6. 左侧“审核完成后”二选一：自动同步到轻推，或生成表格、手动复制；选择会记住。\n"
        "   自动同步模式在达到有效样本目标后上传轻推。同日期更新覆盖，不会重复追加。\n"
        "   手动复制模式不访问轻推、不重试以前的同步任务；审核完成后弹出每日统计表。\n"
        "   表格默认显示当前任务日期，可切换日期；只列出已完成点位，可点“查看统计表”重新打开。\n"
        "   点“复制整张表格”后在 WPS / 轻推中粘贴，可取消包含表头。遮挡、连字两列留空。\n"
        "   表格总计含全部已审核图片；模糊与其他合并列入“其他”，正确率仍只按正确和错误计算。\n"
        "7. 首次上传或轻推登录过期时，在打开的浏览器中扫码 / 手机验证；会话保存在本机。\n"
        "8. 上传失败不丢失审核数据；网络问题会自动重试，也可点“立即同步 / 重试”。\n"
        "9. 本地缓存和审核进度位于 %LOCALAPPDATA%\\AutoAnalyzer，更新 exe 不会清除。\n"
        "10. 瑞丰点位仍需要企业 aTrust / 远程桌面通道就绪，首次远端部署依现场环境完成。\n\n"
        "已内置网站账号、点位和目标文档，以及独立 Chromium 与 Qt 界面组件。无需安装 Google Chrome。\n"
        "企业 VPN 与轻推的交互验证无法用网站密码代替；轻推文档同步目前仍使用内置浏览器。\n",
        encoding="utf-8-sig")
    print(json.dumps({"exe": str(target), "bytes": target.stat().st_size, "sha256": digest, **launcher}))


if __name__ == "__main__":
    main()
