import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import zipfile

import pytest

from scripts.windows_bundle import MAGIC, build_launcher


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    if os.name != "nt": pytest.skip("Windows launcher")
    root = tmp_path_factory.mktemp("launcher with spaces")
    runtime = root / "runtime"
    runtime.mkdir()
    code = root / "core.cs"
    code.write_text('''using System; using System.IO; using System.Threading;
class Core { static int Main(string[] args) {
    using(var handle=EventWaitHandle.OpenExisting(Environment.GetEnvironmentVariable("AUTOANALYZER_VISIBLE_EVENT"))) handle.Set();
    if(args.Length>0) File.WriteAllLines(args[0], args);
    return 0;
} }''', encoding="utf-8")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(runtime / "AutoAnalyzerCore.exe"), str(code)], check=True)
    for name in ("python313.dll", "PySide6/QtCore.pyd", "PySide6/QtWidgets.pyd", "desktop_assets/config.json", "desktop_assets/chromium/chrome.exe"):
        path = runtime / "_internal" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    nested = runtime / "_internal/metadata" / ("resource-" + "x" * 80 + ".json")
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"fixture")
    from PIL import Image
    icon = root / "app.ico"
    Image.new("RGB", (32, 32), "#6658e8").save(icon)
    target = root / "审核工具.exe"
    info = build_launcher(runtime, target, icon, stage=root / "stage")
    return root, target, info


def run(package, exe, report, *args, data_root=None):
    root, _target, _info = package
    env = {**os.environ, "AUTOANALYZER_DATA_DIR": str(data_root or root / "user")}
    result = subprocess.run([str(exe), "--launcher-report", str(report), *map(str, args)], env=env, timeout=25)
    return result.returncode, json.loads(report.read_text(encoding="utf-8-sig"))


def test_window_precedes_payload_and_cached_runtime_recovers_missing_component(package):
    root, exe, info = package
    canary = root / "user" / "review.sqlite3"
    canary.parent.mkdir(parents=True)
    canary.write_bytes(b"existing audits")
    marker = root / "argument report.txt"
    code, cold = run(package, exe, root / "cold.json", marker, 'a "quote"', "C:\\folder\\")
    assert code == 0 and cold["extracted"] and 0 <= cold["shown_ms"] < cold["runtime_ready_ms"]
    assert cold['cache_status'] == 'first'
    assert cold["qt_visible_ms"] >= cold["runtime_ready_ms"]
    assert marker.read_text(encoding="utf-8").splitlines()[1:] == ['a "quote"', "C:\\folder\\"]
    code, warm = run(package, exe, root / "warm.json")
    assert code == 0 and warm["extracted"] is False
    assert warm['cache_status'] == 'cached'
    component = root / "user/app-runtime" / info["payload_sha256"][:32] / "_internal/PySide6/QtWidgets.pyd"
    component.unlink()
    code, repair = run(package, exe, root / "repair.json")
    assert code == 0 and repair["extracted"] and component.is_file()
    assert repair['cache_status'] == 'repair'
    assert canary.read_bytes() == b"existing audits"


def test_repeated_and_renamed_package_reuses_components_without_touching_marker(package):
    root, exe, info = package
    user = root / 'repeat-user'
    code, cold = run(package, exe, root / 'repeat-cold.json', data_root=user)
    assert code == 0 and cold['cache_status'] == 'first'
    marker = user / 'app-runtime' / info['payload_sha256'][:32] / '.complete'
    modified = marker.stat().st_mtime_ns
    renamed = root / '复制到另一个名字.exe'
    import shutil
    shutil.copyfile(exe, renamed)
    for index, target in enumerate((exe, renamed, exe)):
        code, report = run(package, target, root / f'repeat-{index}.json', data_root=user)
        assert code == 0 and not report['extracted'] and report['cache_status'] == 'cached'
        assert marker.stat().st_mtime_ns == modified
    browser = marker.parent / '_internal/desktop_assets/chromium/chrome.exe'
    browser.unlink()
    code, report = run(package, exe, root / 'repair-browser.json', data_root=user)
    assert code == 0 and report['cache_status'] == 'repair' and browser.is_file()


def test_bad_payload_and_traversal_never_execute_or_escape_cache(package):
    root, exe, _info = package
    damaged = root / "damaged.exe"
    damaged.write_bytes(exe.read_bytes()[:-32] + b"\0" * 32)
    code, report = run(package, damaged, root / "damaged.json")
    assert code != 0 and "校验失败" in report["error"]
    payload = root / "traversal.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.txt", "must not write")
    data = payload.read_bytes()
    malicious = root / "traversal.exe"
    malicious.write_bytes((root / "stage/launcher.exe").read_bytes() + data + MAGIC + struct.pack("<q", len(data)) + hashlib.sha256(data).digest())
    code, report = run(package, malicious, root / "traversal.json")
    assert code != 0 and "路径无效" in report["error"]
    assert not (root / "user/app-runtime/escape.txt").exists()


def test_runtime_install_supports_long_cache_paths(package):
    root, exe, info = package
    baseline = len(str(root / "app-runtime" / info["payload_sha256"][:32] / "AutoAnalyzerCore.exe"))
    folder = root / ("a" * max(1, 248 - baseline - 1))
    code, report = run(package, exe, root / "long-path.json", data_root=folder)
    assert code == 0 and report["extracted"]
    assert any(len(str(path)) > 260 for path in folder.rglob("*.json"))
