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
        value = self.data[url] if isinstance(self.data, dict) else self.data
        if isinstance(value, Exception): raise value
        return Response(value)


def metadata(**overrides):
    return {"tag_name": "v1.5.0", "draft": False, "prerelease": False, "body": "New release",
            "assets": [{"name": "AutoAnalyzer.exe", "state": "uploaded", "size": len(PAYLOAD),
                        "digest": "sha256:" + DIGEST, "browser_download_url": URL}], **overrides}


@pytest.mark.parametrize("info,expected", [(metadata(), "1.5.0"), (metadata(tag_name="v1.3.0"), None),
    (metadata(tag_name="v1.4.0"), None), (metadata(prerelease=True), None), (metadata(draft=True), None),
    (metadata(tag_name="nightly"), "error"), (metadata(assets=[]), "error")])
def test_check_only_offers_newer_complete_stable_releases(monkeypatch, info, expected):
    monkeypatch.setattr(updates, "http_session", lambda: Session(json.dumps(info).encode()))
    if expected == 'error':
        with pytest.raises(updates.UpdateMetadataError): updates.check_for_update(CancellationToken(),current='1.4.0')
    else:
        result = updates.check_for_update(CancellationToken(), current="1.4.0")
        assert (result.version if result else None) == expected


def test_network_failure_is_an_optional_skipped_check(monkeypatch):
    def unavailable(): raise OSError("GitHub unavailable")
    monkeypatch.setattr(updates, "http_session", unavailable)
    with pytest.raises(updates.UpdateUnavailable): updates.check_for_update(CancellationToken())


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


def test_cached_runtime_updates_the_original_launcher_not_internal_core(tmp_path, monkeypatch):
    if os.name != "nt": pytest.skip("Windows installer")
    launcher, core, downloaded = (tmp_path / name for name in ("launcher.exe", "core.exe", "new.exe"))
    for path in (launcher, core, downloaded): path.write_bytes(b"MZfixture")
    monkeypatch.setenv("AUTOANALYZER_LAUNCHER_EXE", str(launcher))
    monkeypatch.setattr(updates.sys, "executable", str(core))
    calls = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **_kw: calls.append(args))
    updates.launch_installer(downloaded, updates.Release("1.7.0", URL, DIGEST, 10))
    assert calls[0][calls[0].index("-TargetPath") + 1] == str(launcher.resolve())


def test_required_update_cannot_skip_or_close_into_old_version(monkeypatch,qapp):
    import time
    from threading import Event,current_thread
    from app.desktop.update_dialog import UpdateDialog
    entered,release=Event(),Event();workers=[]
    def download(*_args):
        workers.append(current_thread());entered.set();release.wait(5)
        raise RuntimeError('fixture exit')
    monkeypatch.setattr('app.desktop.update_dialog.download_update',download)
    dialog=UpdateDialog();continued=[];exited=[]
    dialog.continue_startup.connect(lambda:continued.append(True))
    dialog.exit_requested.connect(lambda:exited.append(True))
    dialog._checked(0,updates.Release('9.0.0',URL,DIGEST,len(PAYLOAD)))
    try:
        qapp.processEvents();assert entered.wait(2)
        assert dialog.state=='downloading' and not dialog.skip_button.isVisible()
        dialog.skip();assert not dialog.finished and not continued
        dialog.reject();assert dialog.finished and exited==[True] and not continued
    finally:
        release.set()
        for worker in workers:worker.join(2)


def test_only_offline_error_enables_skip_and_late_results_are_ignored(monkeypatch,qapp):
    from app.desktop.update_dialog import UpdateDialog
    dialog=UpdateDialog();continued=[]
    dialog.continue_startup.connect(lambda:continued.append(True))
    dialog.show();dialog.skip();assert not continued
    dialog._checked(0,None,updates.UpdateUnavailable('offline'))
    assert dialog.state=='offline' and dialog.skip_button.isVisible()
    dialog.skip();assert continued==[True] and dialog.finished
    dialog._checked(0,updates.Release('9.0.0',URL,DIGEST,len(PAYLOAD)))
    assert continued==[True] and not dialog.isVisible()
    another=UpdateDialog();another.show()
    another._failed(0,ValueError('checksum mismatch'))
    another.skip();assert another.state=='error' and not another.finished and not another.skip_button.isVisible()
    another.exit()


def test_check_deadline_offers_offline_only_after_no_response(monkeypatch,qapp):
    import time
    from threading import Event,current_thread
    from app.desktop.update_dialog import UpdateDialog
    entered,release=Event(),Event();workers=[]
    def stuck(_token):workers.append(current_thread());entered.set();release.wait(5)
    monkeypatch.setattr('app.desktop.update_dialog.check_for_update',stuck)
    dialog=UpdateDialog();continued=[]
    dialog.continue_startup.connect(lambda:continued.append(True))
    try:
        dialog.begin();dialog.check_timer.start(50)
        deadline=time.monotonic()+2
        while dialog.state!='offline' and time.monotonic()<deadline:qapp.processEvents();time.sleep(.005)
        assert entered.is_set() and dialog.state=='offline' and not continued
        dialog.skip();assert continued==[True]
    finally:
        release.set()
        for worker in workers:worker.join(2)


def test_api_failure_uses_public_manifest_and_online_invalid_data_never_means_offline(monkeypatch):
    import requests
    manifest={'version':'1.5.0','url':URL,'sha256':DIGEST,'size':len(PAYLOAD)}
    session=Session({updates.LATEST_URL:requests.HTTPError('API rate limit'),updates.LATEST_MANIFEST_URL:json.dumps(manifest).encode()})
    monkeypatch.setattr(updates,'http_session',lambda:session)
    assert updates.check_for_update(CancellationToken(),current='1.4.0').version=='1.5.0'
    session.data[updates.LATEST_MANIFEST_URL]=b'not json'
    with pytest.raises(updates.UpdateMetadataError):updates.check_for_update(CancellationToken(),current='1.4.0')
