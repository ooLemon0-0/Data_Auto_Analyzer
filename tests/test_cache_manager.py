from datetime import date
import os
from pathlib import Path
import subprocess
from threading import Event
import time
from types import SimpleNamespace

import pytest

from app import db
from app.core.cancellation import CancellationToken, WorkCancelled, cancellation_scope, cancel_pause
from app.core.config import settings
from app.core.models import SourceItem
from app.desktop.cache_store import CacheStore
from app.desktop.downloads import DownloadManager, task_key
from app.services.review_service import ReviewService


DAY = date(2026, 11, 4)


def wait(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline: time.sleep(.01)
    assert predicate()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(tmp_path / "data"))
    db.init_db()
    return tmp_path / "data"


def photo(root, project, day, filename, size):
    path = root / project / day / "images" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_scan_sums_dates_projects_originals_and_partial_files_without_other_data(storage):
    photo(storage, "a", str(DAY), "1.png", 120)
    photo(storage, "a", str(DAY), "originals/raw.jpg", 300)
    photo(storage, "b", str(DAY), "2.jpg.part", 80)
    photo(storage, "a", "2026-11-03", "3.jpg", 40)
    export = storage / "a" / str(DAY) / "export.zip"
    export.write_bytes(b"keep archive")
    days = CacheStore(storage).scan()
    assert [(d.day, d.files, d.size) for d in days] == [(str(DAY), 3, 500), ("2026-11-03", 1, 40)]
    assert set(days[0].projects) == {"a", "b"}
    report = CacheStore(storage).clear([str(DAY), "2026-11-03"])
    assert report["removed"] == 4 and report["freed"] == 540 and report["failed"] == 0
    assert export.read_bytes() == b"keep archive" and db.DB_PATH.exists()


def test_clear_rejects_traversal_and_never_follows_junctions(storage, tmp_path):
    external = tmp_path / "outside cache"
    external.mkdir()
    canary = external / "canary.jpg"
    canary.write_bytes(b"keep external image")
    images = storage / "p" / str(DAY) / "images"
    images.mkdir(parents=True)
    junction = images / "outside"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(external)], capture_output=True)
        assert result.returncode == 0
    else:
        junction.symlink_to(external, target_is_directory=True)
    try:
        photo(storage, "p", str(DAY), "local.jpg", 50)
        assert CacheStore(storage).scan()[0].size == 50
        CacheStore(storage).clear([str(DAY)])
        assert canary.read_bytes() == b"keep external image"
        with pytest.raises(ValueError): CacheStore(storage).clear(["../../outside cache"])
    finally:
        if os.name == "nt": junction.rmdir()
        else: junction.unlink()


def test_cancelled_cleanup_preserves_database_and_other_dates(storage):
    photo(storage, "a", str(DAY), "1.jpg", 10)
    photo(storage, "a", str(DAY), "2.jpg", 10)
    other = photo(storage, "a", "2026-11-03", "1.jpg", 20)
    token = CancellationToken()
    with cancellation_scope(token), pytest.raises(WorkCancelled):
        CacheStore(storage).clear([str(DAY)], lambda *_a: token.cancel())
    assert len(list((storage / "a" / str(DAY) / "images").glob("*.jpg"))) == 1
    assert other.exists() and db.DB_PATH.exists()


def test_cleanup_and_resume_keep_queue_decisions_and_other_date_cache(storage, monkeypatch):
    project = SimpleNamespace(id="point", name="point", source={})
    class Source:
        image_cache_revision = ""
        parallel_image_downloads = True
        def __init__(self): self.fetches, self.downloads = [], []
        def fetch_day(self, day):
            self.fetches.append(day)
            return [SourceItem(source_key=str(i), recognition_text=str(i), image_url=f"http://example.invalid/{day}/{i}.jpg") for i in range(4)]
        def materialize_image(self, url, destination):
            self.downloads.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"cached picture")
            return destination
    source, service = Source(), ReviewService()
    config = {"id": "point", "daily_target": 2, "source": {"prefetch_sample_images": True}}
    monkeypatch.setattr(service, "project_dict", lambda _pid: config)
    monkeypatch.setattr("app.services.review_service.build_source", lambda _p: source)
    manager = DownloadManager([project], lambda _j: None, service=service, ensure_route=lambda _p: None,
        state_path=storage.parent / "download_tasks.json")
    other = date(2026, 11, 3)
    try:
        for day in (DAY, other):
            manager.submit(["point"], day, 2)
            wait(lambda: not manager.has_active())
        state = service.state("point", DAY)
        service.decide("point", DAY, state["current"]["queue_id"], "correct")
        with db.connect() as conn:
            before = [tuple(r) for r in conn.execute("SELECT * FROM review_queue ORDER BY id")]
        with manager.cache_cleanup([str(DAY)]):
            CacheStore(storage).clear([str(DAY)])
            with pytest.raises(RuntimeError, match="清理"):
                manager.submit(["point"], DAY, 2)
        assert manager.get(task_key("point", DAY))["status"] == "paused"
        assert manager.get(task_key("point", other))["status"] == "ready"
        with db.connect() as conn:
            assert [tuple(r) for r in conn.execute("SELECT * FROM review_queue ORDER BY id")] == before
        manager.submit(["point"], DAY, 2)
        wait(lambda: not manager.has_active())
        assert manager.get(task_key("point", DAY))["status"] == "ready"
        assert service.state("point", DAY)["valid_count"] == 1
        assert source.fetches == [DAY, other] and len(source.downloads) == 6
        with db.connect() as conn:
            assert [tuple(r) for r in conn.execute("SELECT * FROM review_queue ORDER BY id")] == before
    finally:
        manager.shutdown(cancel=True)


def test_active_download_date_cannot_be_cleaned(storage):
    entered = Event()
    def work(*_a):
        entered.set()
        cancel_pause(20)
    manager = DownloadManager([SimpleNamespace(id="p", name="p", source={})], lambda _j: None,
        service=SimpleNamespace(prepare=work), ensure_route=lambda _p: None)
    cached = photo(storage, "p", str(DAY), "old.jpg", 50)
    try:
        manager.submit(["p"], DAY, 2)
        assert entered.wait(3)
        with pytest.raises(RuntimeError, match="先停止"):
            with manager.cache_cleanup([str(DAY)]):
                CacheStore(storage).clear([str(DAY)])
        assert cached.exists()
    finally:
        manager.shutdown(cancel=True)


def test_file_error_reports_actual_freed_bytes_and_keeps_failed_image(storage, monkeypatch):
    photo(storage, "p", str(DAY), "one.jpg", 50)
    locked = photo(storage, "p", str(DAY), "locked.jpg", 100)
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == locked: raise PermissionError("locked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    report = CacheStore(storage).clear([str(DAY)])
    assert report["removed"] == 1 and report["freed"] == 50 and report["failed"] == 1
    assert locked.exists()
