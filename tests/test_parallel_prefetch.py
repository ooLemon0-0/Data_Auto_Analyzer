from datetime import date
from threading import Barrier

import pytest

from app import db
from app.core.config import settings
from app.core.models import SourceItem
from app.core.progress import progress_scope
from app.projects.binxin_phase2.source import BinxinPhase2HistorySource
from app.services.review_service import ReviewService


DAY = date(2026, 11, 4)


class HttpSource:
    parallel_image_downloads = True
    def __init__(self):
        self.calls = []
        self.fail = False
        self.barrier = None

    def fetch_day(self, day):
        return [SourceItem(source_key=str(i), image_url=f"http://box/{i}.jpg", recognition_text="") for i in range(4)]

    def materialize_image(self, url, destination):
        self.calls.append(url)
        if self.barrier:
            self.barrier.wait()
        if self.fail and url.endswith("/2.jpg"):
            raise RuntimeError("offline")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"cached image")
        return destination


@pytest.fixture
def prefetch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(tmp_path / "data"))
    db.init_db()
    source, service = HttpSource(), ReviewService()
    monkeypatch.setattr(service, "project_dict", lambda _pid: {"id": "parallel", "daily_target": 4,
        "source": {"prefetch_sample_images": True}})
    monkeypatch.setattr("app.services.review_service.build_source", lambda _p: source)
    return source, service


def test_http_images_download_in_parallel_and_reopening_reuses_files(prefetch):
    source, service = prefetch
    source.barrier = Barrier(4, timeout=5)
    events = []
    with progress_scope(events.append):
        result = service.prepare("parallel", DAY, 4)
    assert result["local_images_ready"] == result["local_images_downloaded"] == 4
    assert [e["current"] for e in events if e["stage"] == "images"] == [0, 1, 2, 3, 4]
    assert service.prepare("parallel", DAY, 4)["local_images_downloaded"] == 0
    assert len(source.calls) == 4


def test_failed_image_keeps_successful_files_and_retry_fetches_only_missing(prefetch):
    source, service = prefetch
    source.fail = True
    with pytest.raises(RuntimeError, match="失败 1 条"):
        service.prepare("parallel", DAY, 4)
    assert len(source.calls) == 4
    source.fail = False
    assert service.prepare("parallel", DAY, 4)["local_images_downloaded"] == 1
    assert len(source.calls) == 5


def test_batch_reuses_one_history_query_but_preserves_camera_isolation(monkeypatch):
    source = BinxinPhase2HistorySource({"source": {"base_url": "http://box:3000", "point_name": "point",
        "station": "76", "api": {"page_size": 10}}})
    camera = {"id": "a", "businessGroupId": "group"}
    calls = []
    def query(*_a, **_kw):
        calls.append(True)
        return {"rowCount": 2, "data": [{"id": i, "accTime": 1793764800000 + i,
            "cameraId": cam, "identifyFullUrl": f"http://images/{i}.jpg"} for i, cam in enumerate(("a", "b"))]}
    monkeypatch.setattr(source, "_query", query)
    with progress_scope(lambda _e: None, collection_cache={}):
        a = source._fetch_camera_day(None, camera, DAY)
        b = source._fetch_camera_day(None, {**camera, "id": "b"}, DAY)
    assert len(calls) == 1
    assert [i.metadata["record_id"] for i in a] == [0]
    assert [i.metadata["record_id"] for i in b] == [1]
    with progress_scope(lambda _e: None, collection_cache={}):
        source._fetch_camera_day(None, camera, DAY)
    assert len(calls) == 2
