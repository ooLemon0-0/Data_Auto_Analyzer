from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from app import db
from app.core.config import settings
from app.core.models import SourceItem
from app.desktop.downloads import DownloadManager
from app.projects.binxin_phase2.review_image import COLOR, REVISION, parse_box, render_review
from app.projects.binxin_phase2.source import BinxinPhase2HistorySource
from app.services.review_service import ReviewService


DAY = date(2026, 11, 4)


@pytest.mark.parametrize("value", [None, "bad", "[]", [0, 0, 0, 0], [8, 8, 1, 1], [0, 0, float("nan"), 4]])
def test_invalid_coordinates_are_not_used_to_invent_a_box(value):
    assert parse_box(value) is None


def test_coordinate_json_matches_web_xyxy_and_drawing_keeps_original_unchanged(tmp_path):
    original = tmp_path / "original.png"
    Image.new("RGB", (200, 150), "#223344").save(original)
    before = original.read_bytes()
    destination = tmp_path / "review.png"
    path, metadata = render_review(original, destination,
        {"original_image_url": "http://box/full.png", "review_box": "[20, 40, 80, 100]"}, "ABC123")
    with Image.open(path) as image:
        assert image.size == (200, 150)
        assert image.getpixel((20, 70)) == (44, 182, 179)
        assert image.getpixel((79, 70)) == (44, 182, 179)
        assert image.getpixel((100, 120)) == (34, 51, 68)
    assert metadata["review_image_kind"] == "boxed"
    assert original.read_bytes() == before


def test_full_image_or_coordinates_missing_falls_back_without_fake_overlay(tmp_path):
    original = tmp_path / "raw.png"
    Image.new("RGB", (120, 80), "#223344").save(original)
    for name, data, kind in (("missing-coordinates", {"original_image_url": "http://full", "review_box": None}, "original"),
                             ("crop-only", {"review_box": [2, 3, 20, 30]}, "detail")):
        path, result = render_review(original, tmp_path / (name + ".png"), data, "OCR")
        assert result["review_image_kind"] == kind
        with Image.open(path) as image:
            assert image.getextrema() == ((34, 34), (51, 51), (68, 68))


def test_two_detections_share_only_raw_frame_and_render_their_own_boxes(tmp_path, monkeypatch):
    source = BinxinPhase2HistorySource({"source": {"base_url": "http://box", "point_name": "point"}})
    downloads = []
    def download(url, destination):
        downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200, 150), "#223344").save(destination)
        return destination
    monkeypatch.setattr(source, "materialize_image", download)
    metadata = {"original_image_url": "http://box/shared.png", "review_box": [10, 40, 50, 100]}
    a, _ = source.materialize_review_image("http://box/shared.png", tmp_path / "a.png", metadata, label="ONE")
    b, _ = source.materialize_review_image("http://box/shared.png", tmp_path / "b.png",
        {**metadata, "review_box": [100, 40, 140, 100]}, label="TWO")
    assert len(downloads) == 1
    with Image.open(a) as first, Image.open(b) as second:
        assert first.getpixel((10, 70)) == (44, 182, 179)
        assert second.getpixel((10, 70)) == (34, 51, 68)
        assert second.getpixel((100, 70)) == (44, 182, 179)


def test_old_cache_upgrade_preserves_decisions_queue_and_reuses_raw_images(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(tmp_path / "data"))
    db.init_db()
    pid = "phase2-review-test"
    project = {"id": pid, "daily_target": 2, "source": {"type": "binxin_phase2_history",
        "base_url": "http://box:3000", "point_name": "point", "station": "76", "prefetch_sample_images": True}}
    camera = {"id": "cam", "businessGroupId": "group"}
    records = [{"id": i, "accTime": 1793764800000+i, "cameraId": "cam", "identifyFullUrl": f"http://images/{i}.png",
        "identifyCoordinate": [20+i*10, 40, 70+i*10, 100], "identifyBilletNumber": f"B{i}"} for i in range(3)]
    raw = tmp_path / "old-raw.png"
    Image.new("RGB", (200, 150), "#223344").save(raw)
    original_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    source = BinxinPhase2HistorySource(project)
    db.upsert_items(pid, str(DAY), [SourceItem(source_key=f"76_cam_{i}", recognition_text=f"B{i}", image_path=raw,
        image_url=f"http://images/{i}.png", metadata={"record_id": i}) for i in range(3)])
    service = ReviewService()
    monkeypatch.setattr(service, "project_dict", lambda _pid: {"id": pid, "daily_target": 2, "source": {}})
    old = service.prepare(pid, DAY, 2)
    old = service.decide(pid, DAY, old["current"]["queue_id"], "correct")
    old_ids = [e["queue_id"] for e in old["entries"]]
    monkeypatch.setattr(service, "project_dict", lambda _pid: project)
    fetches = []
    def fetch(self, day):
        fetches.append(day)
        return [self._record_to_item(r, camera, day) for r in records]
    monkeypatch.setattr(BinxinPhase2HistorySource, "fetch_day", fetch)
    monkeypatch.setattr(BinxinPhase2HistorySource, "materialize_image", lambda *_a: pytest.fail("Cached raw image was downloaded again"))
    manager = DownloadManager([SimpleNamespace(id=pid, name=pid, source=project["source"])], lambda _j: None, service=service)
    assert manager.restore_sessions()[0]["status"] == "paused"
    manager.shutdown()
    upgraded = service.prepare(pid, DAY, 2)
    assert upgraded["correct"] == 1 and [e["queue_id"] for e in upgraded["entries"]] == old_ids
    assert all(e["metadata"]["review_image_kind"] == "boxed" for e in upgraded["entries"])
    assert service.materialize_item_image(upgraded["current"]["item_id"]).name.endswith(f".{REVISION}.png")
    assert service.prepare(pid, DAY, 2)["local_images_downloaded"] == 0
    assert len(fetches) == 1 and hashlib.sha256(raw.read_bytes()).hexdigest() == original_hash
