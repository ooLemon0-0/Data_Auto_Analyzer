import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest


def test_real_windows_installer_replaces_restarts_and_keeps_audit_file(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows installer')
    root = Path(__file__).resolve().parents[1]
    compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    source = tmp_path / 'fixture.cs'
    marker = tmp_path / 'restarted.txt'
    literal = str(marker).replace('\\', '\\\\')
    source.write_text('using System.IO; class Program { static void Main() { File.WriteAllText("' + literal + '", "new version"); } }', encoding='utf-8')
    downloaded = tmp_path / 'download' / 'new.exe'
    downloaded.parent.mkdir()
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/out:' + str(downloaded), str(source)], check=True)
    target = tmp_path / 'current app.exe'
    target.write_bytes(b'MZ previous fixture')
    audit = tmp_path / 'review.sqlite3'
    audit.write_bytes(b'preserved audit canary')
    digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
    worker = tmp_path / 'start-installer.py'
    worker.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,' + repr(str(root)) + ')\n'
        'from app.desktop.updates import Release,launch_installer\n'
        'launch_installer(Path(' + repr(str(downloaded)) + '),Release("9.0.0","unused",' + repr(digest) + ',0),executable=Path(' + repr(str(target)) + '))\n', encoding='utf-8')
    subprocess.run([str(root / '.venv/Scripts/python.exe'), '-I', str(worker)], check=True, timeout=15)
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    log = downloaded.parent / 'install.log'
    assert marker.exists(), log.read_text(encoding='utf-8-sig') if log.exists() else 'installer did not restart fixture'
    assert marker.read_text() == 'new version'
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert audit.read_bytes() == b'preserved audit canary'
