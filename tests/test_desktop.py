import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.desktop.bootstrap import bundled_browser, relocate_config
from app.desktop.network import EasyConnectManager
from app.desktop.sync import DesktopSync, fingerprint


def test_desktop_config_relocates_every_writable_path_and_retains_embedded_credentials(tmp_path):
    original = {"storage": {"db_path": "./runtime/old.db"}, "projects": [{
        "id": "point-a", "diagnostics": {"enabled": True},
        "source": {"browser": {"channel": "chrome"}, "auth": {"username": "user", "password": "built-in"}},
        "sink": {"browser": {"channel": "chrome"}}, "cache": {},
    }], "remote_connections": [{"socks_over_rdp": {"source_dir": "./dependencies"}}]}
    snapshot = copy.deepcopy(original)
    browser = tmp_path / "runtime-browser/chrome.exe"
    cfg = relocate_config(original, tmp_path / "user-data", browser)
    assert original == snapshot
    root = tmp_path / "user-data"
    assert Path(cfg["storage"]["db_path"]).is_relative_to(root)
    point = cfg["projects"][0]
    assert point["source"]["auth"]["password"] == "built-in"
    assert point["source"]["prefetch_sample_images"]
    assert not point["diagnostics"]["enabled"]
    for browser_cfg in (point["source"]["browser"], point["sink"]["browser"]):
        assert Path(browser_cfg["user_data_dir"]).is_relative_to(root)
        assert browser_cfg["executable_path"] == str(browser)
        assert "channel" not in browser_cfg
    assert point["sink"]["browser"]["remote_debugging_port"] != 9223
    assert Path(point["cache"]["result_export_dir"]).is_relative_to(root)


def test_packaged_browser_is_cached_outside_onefile_temporary_directory(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    source = bundle / "desktop_assets/chromium"
    source.mkdir(parents=True)
    (source / "chrome.exe").write_bytes(b"browser")
    (source.parent / "build_info.json").write_text(json.dumps({"browser_revision": "test-1"}))
    data = tmp_path / "appdata"
    monkeypatch.setenv("AUTOANALYZER_DATA_DIR", str(data))
    monkeypatch.setattr("app.desktop.bootstrap.resource_root", lambda: bundle)
    target = bundled_browser()
    assert target.is_relative_to(data) and target.read_bytes() == b"browser"
    timestamp = target.stat().st_mtime_ns
    assert bundled_browser() == target
    assert target.stat().st_mtime_ns == timestamp


@pytest.mark.parametrize("running,expected_launches", [(False, 1), (True, 0)])
def test_easyconnect_is_started_only_when_client_is_not_running(tmp_path, monkeypatch, running, expected_launches):
    executable = tmp_path / "SangforCSClient.exe"
    executable.touch()
    manager, launched = EasyConnectManager(), []
    monkeypatch.setattr(manager, "process_names", lambda: {"sangforcsclient.exe"} if running else set())
    monkeypatch.setattr(manager, "candidates", lambda: [executable])
    monkeypatch.setattr("app.desktop.network.subprocess.Popen", lambda args, **_kw: launched.append(args))
    monkeypatch.setattr("app.desktop.network.requests.get", lambda *_a, **_k: SimpleNamespace(status_code=200))
    state = manager.ensure("http://intranet/")
    assert state["available"] and state["running"]
    assert len(launched) == expected_launches


@pytest.fixture
def sync(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    db.init_db()
    state = {"project_id": "point", "business_date": "2026-11-04", "target_size": 2,
             "correct": 1, "incorrect": 1, "invalid": 0, "complete": True}
    service = SimpleNamespace(state=lambda *_args: state, upload=lambda *_args: {"uploaded": True})
    return DesktopSync(service), service, state


def test_upload_is_idempotent_and_changed_statistics_queue_an_upsert(sync):
    queue, service, state = sync
    assert queue.enqueue(state) and not queue.enqueue(state)
    first_digest = fingerprint(state)
    assert queue.run(queue.due())["uploaded"]
    assert queue.due() is None and not queue.enqueue(state)
    state.update(correct=2, incorrect=0)
    assert queue.enqueue(state)
    assert queue.due()["fingerprint"] != first_digest
    assert queue.run(queue.due())["uploaded"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM desktop_sync_jobs").fetchone()[0] == 1


def test_network_failure_is_persisted_and_can_be_retried_after_restart(sync):
    queue, service, state = sync
    queue.enqueue(state)

    def fail(*_args):
        raise RuntimeError("network unreachable")
    service.upload = fail
    with pytest.raises(RuntimeError, match="network unreachable"):
        queue.run(queue.due())
    restarted = DesktopSync(service)
    assert restarted.due() is None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM desktop_sync_jobs").fetchone()
        assert row["status"] == "retry" and row["attempts"] == 1
    service.upload = lambda *_args: {"uploaded": True}
    restarted.retry(state["project_id"], state["business_date"])
    assert restarted.run(restarted.due())["uploaded"]


def test_authentication_failure_waits_for_user_instead_of_reopening_login_repeatedly(sync):
    queue, service, state = sync
    queue.enqueue(state)

    def fail(*_args):
        raise RuntimeError("轻推扫码登录超时")
    service.upload = fail
    with pytest.raises(RuntimeError, match="扫码"):
        queue.run(queue.due())
    assert queue.due() is None
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM desktop_sync_jobs").fetchone()[0] == "auth_required"


def test_interrupted_upload_is_recovered_and_incomplete_review_cancels_pending_sync(sync):
    queue, service, state = sync
    queue.enqueue(state)
    with db.connect() as conn:
        conn.execute("UPDATE desktop_sync_jobs SET status='running'")
    queue = DesktopSync(service)
    assert queue.due() is not None
    state["complete"] = False
    assert not queue.enqueue(state) and queue.due() is None


def test_upload_rechecks_current_statistics_before_sending(sync):
    queue, service, state = sync
    queue.enqueue(state)
    stale = queue.due()
    state.update(correct=2, incorrect=0)
    sent = []
    service.upload = lambda *_a: sent.append(True)
    result = queue.run(stale)
    assert result["stale"] and not sent
    assert queue.due()["fingerprint"] == fingerprint(state)


def test_manual_upload_still_runs_when_auto_upload_is_unchecked():
    from app.desktop.window import ReviewWindow
    # This tests scheduler decisions, so avoid an uninitialized Qt object.
    window = SimpleNamespace()
    window.closed, window.busy = False, False
    window.notice = None
    window.selection_dialog = None
    window.auto_upload = SimpleNamespace(isChecked=lambda: False)
    window.sync = SimpleNamespace(due=lambda: {"project_id": "point"})
    runs = []
    window._run = lambda *_args, **_kwargs: runs.append(True)
    ReviewWindow._try_sync(window)
    assert not runs
    ReviewWindow._try_sync(window, force=True)
    assert runs == [True]


def test_manual_retry_prioritizes_the_selected_day_over_older_pending_results(sync):
    queue, service, state = sync
    queue.enqueue(state)
    selected = {**state, "business_date": "2026-11-05"}
    queue.enqueue(selected)
    assert queue.due()["business_date"] == "2026-11-04"
    queue.retry(selected["project_id"], selected["business_date"])
    assert queue.due()["business_date"] == "2026-11-05"
