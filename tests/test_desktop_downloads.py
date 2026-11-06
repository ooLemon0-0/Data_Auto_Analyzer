from datetime import date
from threading import Event, Lock
import time
from types import SimpleNamespace

from app import db
from app.core.models import SourceItem
from app.desktop.downloads import DownloadManager, task_key
from app.services.review_service import ReviewService


DAY = date(2026, 11, 4)


def project(pid, host):
    return SimpleNamespace(id=pid, name=pid, source={"base_url": f"http://{host}:3000"})


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_points_run_concurrently_without_one_host_consuming_all_slots():
    release = Event()
    entered_a, entered_b = Event(), Event()
    calls, active, peak = [], {}, [0]
    lock = Lock()
    def prepare(pid, day, target):
        host = "same" if pid in {"a", "a2"} else "other"
        with lock:
            assert not active.get(host)
            active[host] = True
            peak[0] = max(peak[0], len(active))
            calls.append(pid)
        if pid == "a":
            entered_a.set()
        if pid == "b":
            entered_b.set()
            assert release.wait(5)
        else:
            assert entered_b.wait(5)
        with lock:
            active.pop(host)
    manager = DownloadManager([project("a", "same"), project("a2", "same"), project("b", "other")],
        lambda _j: None, service=SimpleNamespace(prepare=prepare), max_projects=2, ensure_route=lambda _p: None)
    try:
        manager.submit(["a", "a2", "b"], DAY, 50)
        assert entered_a.wait(3) and entered_b.wait(3)
        wait_for(lambda: manager.get(task_key("a", DAY))["status"] == "ready")
        assert manager.get(task_key("b", DAY))["status"] == "running"
        assert peak[0] == 2
        assert manager.submit(["b"], DAY, 100) == []  # Cannot mutate a live queue's target.
        assert calls.count("b") == 1
    finally:
        release.set()
        wait_for(lambda: not manager.has_active())
        manager.shutdown()


def test_one_failed_point_does_not_block_others_and_retry_is_independent():
    fail = [True]
    def prepare(pid, *_a):
        if pid == "bad" and fail[0]:
            raise RuntimeError("temporary failure")
    manager = DownloadManager([project("bad", "one"), project("good", "two")], lambda _j: None,
        service=SimpleNamespace(prepare=prepare), ensure_route=lambda _p: None)
    manager.submit(["bad", "good"], DAY, 2)
    wait_for(lambda: not manager.has_active())
    assert manager.get(task_key("bad", DAY))["status"] == "failed"
    assert manager.get(task_key("good", DAY))["status"] == "ready"
    fail[0] = False
    manager.submit(["bad"], DAY, 2)
    wait_for(lambda: not manager.has_active())
    assert manager.get(task_key("bad", DAY))["status"] == "ready"
    assert manager.submit(["good"], DAY, 2) == []
    manager.shutdown()


def test_existing_reviews_restore_without_network_and_incomplete_cache_waits_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    db.init_db()
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"existing")
    service = ReviewService()
    service.project_dict = lambda pid: {"id": pid, "daily_target": 1, "source": {}}
    for pid in ("local", "missing"):
        db.upsert_items(pid, str(DAY), [SourceItem(source_key=pid, image_path=image if pid == "local" else None,
                                                image_url="http://no-network/image.jpg", recognition_text="")])
        service.prepare(pid, DAY, 1)
    manager = DownloadManager([project("local", "one"), project("missing", "two")], lambda _j: None,
        service=service, ensure_route=lambda _p: (_ for _ in ()).throw(AssertionError("restore used network")))
    jobs = {j["project_id"]: j for j in manager.restore_sessions()}
    assert jobs["local"]["status"] == "ready"
    assert jobs["missing"]["status"] == "paused"
    assert not manager.has_active()
    manager.shutdown()
