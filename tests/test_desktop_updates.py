import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.cancellation import CancellationToken, WorkCancelled
from app.desktop import updates


URL = f"https://github.com/{updates.REPOSITORY}/releases/download/v1.5.0/AutoAnalyzer.exe"
PAYLOAD = b"MZ" + b"test executable" * 100
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def raise_for_status(self): pass
    def iter_content(self, _size):
        for offset in range(0, len(self.data), 100):
            yield self.data[offset:offset + 100]


class Session:
    def __init__(self, data): self.headers, self.data, self.urls = {}, data, []
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def get(self, url, **_kw):
        self.urls.append(url)
        return Response(self.data[url] if isinstance(self.data, dict) else self.data)


def metadata(**overrides):
    return {"tag_name": "v1.5.0", "draft": False, "prerelease": False, "body": "New release",
            "assets": [{"name": "AutoAnalyzer.exe", "state": "uploaded", "size": len(PAYLOAD),
                        "digest": "sha256:" + DIGEST, "browser_download_url": URL}], **overrides}


@pytest.mark.parametrize("info,expected", [(metadata(), "1.5.0"), (metadata(tag_name="v1.3.0"), None),
    (metadata(tag_name="v1.4.0"), None), (metadata(prerelease=True), None), (metadata(draft=True), None),
    (metadata(tag_name="nightly"), None), (metadata(assets=[]), None)])
def test_check_only_offers_newer_complete_stable_releases(monkeypatch, info, expected):
    monkeypatch.setattr(updates, "http_session", lambda: Session(json.dumps(info).encode()))
    result = updates.check_for_update(CancellationToken(), current="1.4.0")
    assert (result.version if result else None) == expected


def test_network_failure_is_an_optional_skipped_check(monkeypatch):
    def unavailable(): raise OSError("GitHub unavailable")
    monkeypatch.setattr(updates, "http_session", unavailable)
    assert updates.check_for_update(CancellationToken()) is None


def test_missing_api_digest_uses_release_checksum_and_rejects_external_urls(monkeypatch):
    info = metadata()
    info["assets"][0].pop("digest")
    checksum_url = URL.replace("AutoAnalyzer.exe", "SHA256SUMS.txt")
    info["assets"].append({"name": "SHA256SUMS.txt", "browser_download_url": checksum_url})
    session = Session({updates.LATEST_URL: json.dumps(info).encode(), checksum_url: f"{DIGEST}  AutoAnalyzer.exe\n".encode()})
    monkeypatch.setattr(updates, "http_session", lambda: session)
    assert updates.check_for_update(CancellationToken(), current="1.4.0").sha256 == DIGEST
    info["assets"][0]["browser_download_url"] = "https://example.org/AutoAnalyzer.exe"
    session.data[updates.LATEST_URL] = json.dumps(info).encode()
    assert updates.check_for_update(CancellationToken()) is None


def test_download_checks_size_sha_and_executable_before_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "http_session", lambda: Session(PAYLOAD))
    release = updates.Release("1.5.0", URL, DIGEST, len(PAYLOAD))
    counts = []
    saved = updates.download_update(release, CancellationToken(), lambda n, total: counts.append((n, total)), tmp_path)
    assert saved.read_bytes() == PAYLOAD and counts[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert not list(tmp_path.rglob("*.part"))
    before = saved.read_bytes()
    monkeypatch.setattr(updates, "http_session", lambda: Session(b"MZ" + b"X" * (len(PAYLOAD) - 2)))
    with pytest.raises(ValueError, match="校验"):
        updates.download_update(release, CancellationToken(), lambda *_a: None, tmp_path)
    assert saved.read_bytes() == before
    assert not list(tmp_path.rglob("*.part"))


def test_skip_during_download_removes_partial_and_preserves_current_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "http_session", lambda: Session(PAYLOAD))
    token = CancellationToken()
    current = tmp_path / "current.exe"
    current.write_bytes(b"MZcurrent version")
    with pytest.raises(WorkCancelled):
        updates.download_update(updates.Release("1.5.0", URL, DIGEST, len(PAYLOAD)), token,
                                lambda *_a: token.cancel(), tmp_path / "updates")
    assert current.read_bytes() == b"MZcurrent version"
    assert not list((tmp_path / "updates").rglob("*.part"))
    assert not list((tmp_path / "updates").rglob("*.exe"))


def test_windows_installer_receives_literal_paths_and_checksum(tmp_path, monkeypatch):
    if os.name != "nt": pytest.skip("Windows installer")
    current = tmp_path / "app with space.exe"
    downloaded = tmp_path / "new.exe"
    current.write_bytes(b"old")
    downloaded.write_bytes(PAYLOAD)
    calls = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    updates.launch_installer(downloaded, updates.Release("1.5.0", URL, DIGEST, len(PAYLOAD)), executable=current)
    args, kwargs = calls[0]
    assert args[args.index("-TargetPath") + 1] == str(current.resolve())
    assert args[args.index("-ExpectedHash") + 1] == DIGEST
    assert kwargs["creationflags"] == updates.subprocess.CREATE_NO_WINDOW
    assert current.read_bytes() == b"old" and not list(tmp_path.glob(".autoanalyzer*"))


def test_update_dialog_skip_ignores_late_result_and_can_cancel_download(monkeypatch, tmp_path, qapp):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.desktop.update_dialog import UpdateDialog
    from app.desktop.theme import apply_theme
    app = qapp
    dialog = UpdateDialog()
    continued = []
    dialog.continue_startup.connect(lambda: continued.append(True))
    dialog.show()
    dialog.skip_button.click()
    dialog._checked(updates.Release("1.5.0", URL, DIGEST, len(PAYLOAD)))
    assert continued == [True] and dialog.token.cancelled and not dialog.isVisible()
    dialog.skip()
    assert continued == [True]
    another = UpdateDialog()
    another._checked(updates.Release("1.5.0", URL, DIGEST, len(PAYLOAD)))
    another.show()
    app.processEvents()
    assert another.update_button.isVisible()
    another.reject()
    assert another.finished and another.token.cancelled


def test_update_ui_deadline_skips_even_when_transport_never_returns(monkeypatch, qapp):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time
    from threading import Event, current_thread
    from PySide6.QtWidgets import QApplication
    from app.desktop.update_dialog import UpdateDialog
    app = qapp
    blocked, entered = Event(), Event()
    workers = []
    def stuck(_token):
        workers.append(current_thread())
        entered.set()
        blocked.wait(10)
    monkeypatch.setattr("app.desktop.update_dialog.check_for_update", stuck)
    dialog = UpdateDialog()
    continued = []
    dialog.continue_startup.connect(lambda: continued.append(True))
    try:
        dialog.begin()
        dialog.check_timer.start(50)
        deadline = time.monotonic() + 2
        while not continued and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert entered.is_set() and continued == [True] and dialog.token.cancelled
    finally:
        blocked.set()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
