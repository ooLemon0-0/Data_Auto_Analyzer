from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from app import db
from app.core.cancellation import CancellationToken, WorkCancelled, cancel_pause, cancellation_scope
from app.core.http import http_get
from app.core.models import SourceItem
from app.desktop.downloads import DownloadManager, task_key
from app.services.review_service import ReviewService
from app.core.config import settings


DAY = date(2026, 11, 4)


def point(pid="point"):
    return SimpleNamespace(id=pid, name=pid, source={"base_url": "http://one-box:3000"})


def wait(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


@pytest.mark.parametrize("body_started", [False, True])
def test_cancel_closes_a_real_slow_http_request_before_timeout(monkeypatch, body_started):
    entered, release, finished = Event(), Event(), Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass
        def do_GET(self):
            if body_started:
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"first")
                self.wfile.flush()
            entered.set()
            release.wait(8)
            try:
                if not body_started:
                    self.send_response(200)
                    self.end_headers()
                self.wfile.write(b"done")
            except (OSError, ConnectionError):
                pass
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
    token, results = CancellationToken(), []
    def request():
        try:
            with cancellation_scope(token):
                http_get(f"http://127.0.0.1:{server.server_port}/slow", timeout=60)
        except Exception as exc:
            results.append(exc)
        finally:
            finished.set()
    thread = Thread(target=request, daemon=True)
    thread.start()
    try:
        assert entered.wait(4)
        start = time.monotonic()
        token.cancel()
        assert finished.wait(2), "Cancellation waited for the 60-second HTTP timeout"
        assert time.monotonic()-start < 2 and isinstance(results[0], WorkCancelled)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_cancel_running_and_queued_jobs_then_restore_and_continue(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    db.init_db()
    entered, calls = Event(), []
    def prepare(pid, *_a):
        calls.append(pid)
        entered.set()
        cancel_pause(30)
    state_path = tmp_path / "tasks.json"
    service = SimpleNamespace(prepare=prepare)
    manager = DownloadManager([point("a"), point("b")], lambda _j: None, service=service,
        max_projects=1, ensure_route=lambda _p: None, state_path=state_path)
    manager.submit(["a", "b"], DAY, 2)
    assert entered.wait(3)
    assert manager.get(task_key("b", DAY))["status"] == "queued"
    manager.cancel()
    wait(lambda: not manager.has_active())
    assert calls == ["a"]
    assert {j["status"] for j in manager.snapshots()} == {"cancelled"}
    manager.shutdown()
    service.prepare = lambda pid, *_a: calls.append(pid)
    restored = DownloadManager([point("a"), point("b")], lambda _j: None, service=service,
        ensure_route=lambda _p: None, state_path=state_path)
    assert {j["status"] for j in restored.restore_sessions()} == {"cancelled"}
    restored.submit(["a"], DAY, 2)
    wait(lambda: not restored.has_active())
    assert restored.get(task_key("a", DAY))["status"] == "ready" and calls == ["a", "a"]
    restored.shutdown()


def test_cancelled_cache_keeps_finished_files_and_continues_without_resampling(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(tmp_path / "images"))
    db.init_db()
    saved = Event()
    class Source:
        parallel_image_downloads = True
        def __init__(self):
            self.calls, self.fetches, self.block = [], 0, True
        def fetch_day(self, day):
            self.fetches += 1
            return [SourceItem(source_key=str(i), recognition_text=str(i), image_url=f"http://images/{i}.jpg") for i in range(2)]
        def materialize_image(self, url, destination):
            self.calls.append(url)
            if url.endswith("/1.jpg") and self.block:
                cancel_pause(30)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"complete cached image")
            return destination
    source, service = Source(), ReviewService()
    config = {"id": "point", "daily_target": 2, "source": {"type": "test", "prefetch_sample_images": True}}
    monkeypatch.setattr(service, "project_dict", lambda _p: config)
    monkeypatch.setattr("app.services.review_service.build_source", lambda _p: source)
    def changed(job):
        if job.get("images_ready") == 1:
            saved.set()
    manager = DownloadManager([point()], changed, service=service, ensure_route=lambda _p: None)
    manager.submit(["point"], DAY, 2)
    assert saved.wait(4)
    before = service.state("point", DAY)
    manager.cancel()
    wait(lambda: not manager.has_active())
    assert manager.get(task_key("point", DAY))["status"] == "cancelled"
    source.block = False
    manager.submit(["point"], DAY, 2)
    wait(lambda: not manager.has_active())
    after = service.state("point", DAY)
    assert [e["queue_id"] for e in before["entries"]] == [e["queue_id"] for e in after["entries"]]
    assert source.fetches == 1 and source.calls.count("http://images/0.jpg") == 1
    assert manager.get(task_key("point", DAY))["images_ready"] == 2
    manager.shutdown()


def test_exit_does_not_wait_for_an_unresponsive_download_thread(tmp_path):
    script = '''
from threading import Event
from types import SimpleNamespace
from datetime import date
import json,time
from app.desktop.downloads import DownloadManager
entered=Event()
def stuck(*_a):
    entered.set()
    Event().wait(90)
p=SimpleNamespace(id='exit-test',name='exit-test',source={'base_url':'http://box'})
m=DownloadManager([p],lambda _j:None,service=SimpleNamespace(prepare=stuck),ensure_route=lambda _p:None)
m.submit(['exit-test'],date(2026,11,4),2)
assert entered.wait(2)
start=time.monotonic()
m.shutdown(cancel=True)
print(json.dumps({'closed_ms':round((time.monotonic()-start)*1000)}),flush=True)
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["closed_ms"] < 1000
