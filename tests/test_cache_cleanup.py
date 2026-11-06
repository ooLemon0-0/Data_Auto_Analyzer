from datetime import date
from threading import Event, get_ident
import time
from types import SimpleNamespace

import pytest

from app import db
from app.core.config import settings
from app.core.models import SourceItem
from app.desktop.cache_cleanup import CACHE_LIMIT_BYTES, CacheMaintenance
from app.desktop.cache_store import CacheStore
from app.desktop.downloads import DownloadManager, task_key


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(root))
    db.init_db()
    manager = DownloadManager([SimpleNamespace(id="p", name="p", source={})], lambda _j: None,
        state_path=tmp_path / "download_tasks.json")
    yield root, manager
    manager.shutdown(cancel=True)


def photo(root, day, size, project="p"):
    path = root / project / day / "images" / "sample.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def wait(predicate, app=None):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        if app:
            app.processEvents()
        time.sleep(.01)
    assert predicate()


def test_ten_gib_limit_and_no_deletion_below_threshold(cache):
    root, manager = cache
    image = photo(root, "2026-11-01", 99)
    assert CACHE_LIMIT_BYTES == 10 * 1024 ** 3
    result = CacheMaintenance(CacheStore(root), manager, limit=100).trim()
    assert result["dates"] == [] and image.exists() and result["remaining"] == 99


def test_threshold_clears_oldest_whole_date_across_points_only(cache):
    root, manager = cache
    oldest = [photo(root, "2026-11-01", 25, p) for p in ("p", "other")]
    newest = photo(root, "2026-11-02", 50)
    export = newest.parent.parent / "statistics.json"
    export.write_text("keep", encoding="utf-8")
    database = db.DB_PATH.read_bytes()
    result = CacheMaintenance(CacheStore(root), manager, limit=100).trim()
    assert result == {"dates": ["2026-11-01"], "removed": 2, "freed": 50, "failed": 0, "remaining": 50}
    assert all(not p.exists() for p in oldest) and newest.exists()
    assert export.read_text() == "keep" and db.DB_PATH.read_bytes() == database


def test_cleans_days_in_order_until_below_limit(cache):
    root, manager = cache
    for day in ("2026-11-03", "2026-11-01", "2026-11-02"):
        photo(root, day, 60)
    result = CacheMaintenance(CacheStore(root), manager, limit=100).trim()
    assert result["dates"] == ["2026-11-01", "2026-11-02"] and result["remaining"] == 60
    assert CacheStore(root).scan()[0].day == "2026-11-03"


def test_download_and_review_claims_after_scan_are_protected(cache, monkeypatch):
    root, manager = cache
    paths = [photo(root, f"2026-11-0{n}", 50) for n in range(1, 4)]
    store = CacheStore(root)
    scan = store.scan
    def racing_scan():
        result = scan()
        assert manager.protect_review_dates({"2026-11-01"})
        manager.jobs["pending"] = {"business_date": "2026-11-02", "status": "queued"}
        return result
    monkeypatch.setattr(store, "scan", racing_scan)
    monkeypatch.setattr(manager, "_pump", lambda: None)
    try:
        result = CacheMaintenance(store, manager, limit=100).trim()
        assert result["dates"] == ["2026-11-03"] and result["remaining"] == 100
        assert paths[0].exists() and paths[1].exists() and not paths[2].exists()
        # When nothing is eligible, defer instead of touching protected pictures.
        assert CacheMaintenance(store, manager, limit=100).trim()["dates"] == []
    finally:
        manager.jobs.clear()


def test_locked_files_retry_on_next_pass_without_losing_other_data(cache, monkeypatch):
    root, manager = cache
    locked = photo(root, "2026-11-01", 60)
    newer = photo(root, "2026-11-02", 60)
    from pathlib import Path
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == locked:
            raise PermissionError("busy image")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        result = CacheMaintenance(CacheStore(root), manager, limit=100).trim()
    assert result["failed"] == 1 and result["freed"] == 60
    assert locked.exists() and not newer.exists()
    photo(root, "2026-11-03", 50)
    assert CacheMaintenance(CacheStore(root), manager, limit=100).trim()["dates"] == ["2026-11-01"]


def test_worker_is_single_background_thread_and_can_stop_without_waiting(cache, monkeypatch):
    root, manager = cache
    for day in ("2026-11-01", "2026-11-02"):
        photo(root, day, 60)
    entered, release = Event(), Event()
    calls, finished = [], []
    store = CacheStore(root)
    original = store.clear
    def clear(*args, **kwargs):
        calls.append(get_ident())
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "clear", clear)
    cleaner = CacheMaintenance(store, manager, limit=100, finished=finished.append)
    main_thread = get_ident()
    try:
        assert cleaner.start() and entered.wait(3)
        assert not cleaner.start()
        assert cleaner.thread.daemon and calls == [cleaner.thread.ident] and calls[0] != main_thread
        start = time.monotonic()
        cleaner.close()
        assert time.monotonic() - start < .2
        assert not cleaner.start()
    finally:
        release.set()
        cleaner.thread.join(5)
    assert not cleaner.running and not finished and not manager.cleaning_dates
    assert len(CacheStore(root).scan()) == 2


def test_cleanup_failure_is_silent_and_retried(cache, monkeypatch):
    root, manager = cache
    store = CacheStore(root)
    finished = []
    cleaner = CacheMaintenance(store, manager, limit=100, finished=finished.append)
    def fail():
        raise PermissionError("temporary disk error")
    monkeypatch.setattr(store, "scan", fail)
    assert cleaner.start()
    cleaner.thread.join(5)
    assert not cleaner.running and not finished
    monkeypatch.setattr(store, "scan", lambda: [])
    assert cleaner.start()
    cleaner.thread.join(5)
    assert not cleaner.running and not finished


def test_download_requested_during_background_cleanup_queues_without_blocking(cache):
    _root, manager = cache
    entered = Event()
    manager.ensure_route = lambda _p: None
    manager.service.prepare = lambda *_a: (entered.set(), {"target_size": 2})[-1]
    day = date(2026, 11, 1)
    with manager.cache_cleanup([str(day)], background=True):
        assert not manager.protect_review_dates({str(day)})
        keys = manager.submit(["p"], day, 2)
        assert manager.get(keys[0])["status"] == "queued" and not entered.is_set()
        assert "缓存" in manager.get(keys[0])["message"]
    assert entered.wait(3)
    wait(lambda: not manager.has_active())
    assert manager.get(keys[0])["status"] == "ready"


def test_cleanup_and_resume_preserve_review_queue_and_decisions(cache, monkeypatch):
    root, manager = cache
    service = manager.service
    config = {"id": "p", "daily_target": 2, "source": {"prefetch_sample_images": True}}
    downloads = []
    class Source:
        image_cache_revision = ""
        parallel_image_downloads = True
        def fetch_day(self, _day):
            return [SourceItem(source_key=str(i), recognition_text=str(i),
                image_url=f"http://example.invalid/{i}.jpg") for i in range(4)]
        def materialize_image(self, url, destination):
            downloads.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"cached picture")
            return destination
    monkeypatch.setattr(service, "project_dict", lambda _pid: config)
    monkeypatch.setattr("app.services.review_service.build_source", lambda _p: Source())
    manager.ensure_route = lambda _p: None
    day = date(2026, 11, 1)
    manager.submit(["p"], day, 2)
    wait(lambda: not manager.has_active())
    state = service.state("p", day)
    service.decide("p", day, state["current"]["queue_id"], "correct")
    with db.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM review_queue ORDER BY id")]
    result = CacheMaintenance(CacheStore(root), manager, limit=1).trim()
    assert result["removed"] == 2 and manager.get(task_key("p", day))["status"] == "paused"
    manager.submit(["p"], day, 2)
    wait(lambda: not manager.has_active())
    assert manager.get(task_key("p", day))["status"] == "ready"
    assert service.state("p", day)["valid_count"] == 1 and len(downloads) == 4
    with db.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM review_queue ORDER BY id")] == before


def test_background_cleanup_keeps_desktop_responsive_and_exits_immediately(qapp, cache, monkeypatch):
    from app.desktop.window import ReviewWindow
    root, _manager = cache
    oldest = photo(root, "2026-11-01", 60)
    current = photo(root, "2026-11-02", 60)
    entered, release = Event(), Event()
    window = ReviewWindow(check_network=False)
    window.auto_upload.setChecked(False)
    window.show()
    window.cache_maintenance.limit = 100
    original = window.cache_maintenance.store.clear
    def clear(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(window.cache_maintenance.store, "clear", clear)
    assert window.downloads.protect_review_dates({"2026-11-02"})
    status = window.status.text()
    try:
        window._check_cache_limit()
        assert entered.wait(3)
        qapp.processEvents()
        assert window.cache_timer.isActive()
        assert window.cache_maintenance.running and not window.busy
        assert window.fetch_button.isEnabled() and window.cache_button.isEnabled()
        assert window.progress_dialog is None and window.notice is None and window.status.text() == status
        window.close()
        assert window.closed and not window.isVisible() and window.exit_dialog is None
        assert not window.cache_timer.isActive() and window.cache_maintenance.token.cancelled
    finally:
        release.set()
        window.cache_maintenance.thread.join(5)
        window._shutdown()
    assert oldest.exists() and current.exists()
