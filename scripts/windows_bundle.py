"""Wrap an onedir runtime in a small Windows loader with a cached ZIP payload."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zipfile

MAGIC = b"AARUN001"


def build_launcher(runtime, target, icon, *, stage):
    root = Path(__file__).resolve().parents[1]
    runtime, target, stage = Path(runtime), Path(target), Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    payload = stage / "runtime.zip"
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(runtime.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(runtime).as_posix())
    return wrap_payload(payload, target, icon, stage=stage)


def wrap_payload(payload, target, icon, *, stage):
    root = Path(__file__).resolve().parents[1]
    payload, target, stage = Path(payload), Path(target), Path(stage)
    with payload.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").digest()
    compiler = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    stub = stage / "launcher.exe"
    subprocess.run([str(compiler), "/nologo", "/codepage:65001", "/target:winexe", "/platform:x64", "/optimize+",
        "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", "/reference:System.IO.Compression.dll",
        "/resource:" + str(icon) + ",AppIcon",
        "/win32icon:" + str(icon), "/win32manifest:" + str(root / "scripts/windows_launcher.manifest"),
        "/out:" + str(stub), str(root / "scripts/windows_launcher.cs")], check=True)
    native = build_preloader(stub, icon, stage=stage)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output, native.open("rb") as header, payload.open("rb") as content:
        shutil.copyfileobj(header, output)
        shutil.copyfileobj(content, output)
        output.write(MAGIC + struct.pack("<q", payload.stat().st_size) + digest)
    return {"payload_sha256": digest.hex(), "payload_bytes": payload.stat().st_size}


def build_preloader(loader, icon, *, stage):
    root = Path(__file__).resolve().parents[1]
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    installation = Path(subprocess.check_output([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True).strip())
    vc = sorted((installation / "VC/Tools/MSVC").iterdir())[-1]
    kit = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10"
    sdk = sorted((kit / "Lib").iterdir())[-1].name
    rc = kit / "bin" / sdk / "x64/rc.exe"
    if not rc.exists():
        rc = sorted((kit / "bin").glob("*/x64/rc.exe"))[-1]
    script, resource, native = stage / "preloader.rc", stage / "preloader.res", stage / "preloader.exe"
    loader_id = hashlib.sha256(loader.read_bytes()).hexdigest()[:32]
    def literal(path): return str(Path(path).resolve()).replace("\\", "\\\\")
    script.write_text(f'1 RCDATA "{literal(loader)}"\n2 ICON "{literal(icon)}"\nSTRINGTABLE\nBEGIN\n 3 "{loader_id}"\nEND\n', encoding="utf-8")
    subprocess.run([str(rc), "/nologo", "/fo", str(resource), str(script)], check=True)
    build_env = {**os.environ, "PATH": os.pathsep.join([str(vc / "bin/Hostx64/x64"), str(rc.parent), os.environ.get("PATH", "")])}
    subprocess.run([str(vc / "bin/Hostx64/x64/cl.exe"), "/nologo", "/O2", "/MT", "/EHsc", "/utf-8", "/DUNICODE", "/D_UNICODE",
        "/I" + str(vc / "include"), *["/I" + str(kit / "Include" / sdk / part) for part in ("ucrt", "shared", "um")],
        "/Fo" + str(stage / "preloader.obj"), "/Fe" + str(native), str(root / "scripts/windows_preloader.cpp"), str(resource),
        "/link", "/SUBSYSTEM:WINDOWS", "/MANIFEST:EMBED", "/MANIFESTINPUT:" + str(root / "scripts/windows_launcher.manifest"),
        "/LIBPATH:" + str(vc / "lib/x64"), "/LIBPATH:" + str(kit / "Lib" / sdk / "um/x64"), "/LIBPATH:" + str(kit / "Lib" / sdk / "ucrt/x64"),
        "user32.lib", "gdi32.lib", "kernel32.lib", "shell32.lib", "comctl32.lib", "dwmapi.lib"], check=True, env=build_env)
    return native
