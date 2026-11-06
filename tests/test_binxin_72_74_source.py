import copy
import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.projects.binxin_72_79.source import Binxin7279HistorySource
from app.projects.binxin_74_71.source import Binxin7471HistorySource


DAY = date(2026, 11, 4)


@pytest.fixture(params=[(Binxin7279HistorySource, "72"), (Binxin7471HistorySource, "74")])
def source(request):
    cls, station = request.param
    return cls({"source": {"station": station, "api": {"page_size": 2}}})


def record(index=0):
    return {
        "id": str(index),
        "accTime": 1793764800000 + index * 1000,
        "identifyPictureUrl": f"http://box/review-{index}.jpg",
        "identifyFullUrl": f"http://box/original-{index}.jpg",
        "identifyBilletNumber": "ABC123" if index else "",
        "identifyBilletNumberReal": "HRB400-ABC123",
        "identifyStatusEnum": "IDENTIFIED",
    }


def test_blank_credentials_inherit_73_and_explicit_credentials_override(source, monkeypatch):
    monkeypatch.setattr(
        type(settings), "project", lambda _self, pid: SimpleNamespace(source={"auth": {
            "username": "shared-user", "password": "shared-password"
        }}) if pid == "binxin_billet" else None,
    )
    assert source._credential("username") == "shared-user"
    assert source._credential("password") == "shared-password"
    source.source["auth"] = {"username": "own-user", "password": "own-password"}
    assert source._credential("username") == "own-user"
    assert source._credential("password") == "own-password"
    source.source["auth"] = {"credentials_from_project": "", "username": None}
    assert source._credential("username") == ""


def test_record_preserves_dom_source_key_review_image_and_empty_ocr(source):
    item = source._api_record_to_item(record(), DAY, 2, 1)
    material = "2026/11/04 12:00:00|review-0.jpg||http://box/review-0.jpg"
    digest = hashlib.sha1(material.encode()).hexdigest()[:16]
    assert item.source_key == f"20261104_120000_{digest}"
    assert item.recognition_text == ""
    assert item.image_url == "http://box/review-0.jpg"
    assert item.metadata["original_image_url"] == "http://box/original-0.jpg"
    assert item.metadata["true_value"] == "HRB400-ABC123"
    assert item.metadata["station"] == source.source["station"]


def test_china_business_date_includes_last_millisecond_and_rejects_other_days(source):
    lower, upper = source._day_bounds_ms(DAY)
    assert (lower, upper) == (1793721600000, 1793807999999)
    last = {**record(), "accTime": upper}
    assert source._api_record_to_item(last, DAY, 1, 1).metadata["timestamp"].endswith("23:59:59")
    with pytest.raises(RuntimeError, match="目标日期之外"):
        source._api_record_to_item({**last, "accTime": upper + 1}, DAY, 1, 1)
    with pytest.raises(RuntimeError, match="缺少识别图片"):
        source._api_record_to_item({"accTime": lower}, DAY, 1, 1)


class ApiPage:
    def __init__(self, source, batches, counts):
        lower, upper = source._day_bounds_ms(DAY)
        self.request = SimpleNamespace(
            url="http://box/api/v1/history/condition-query?currPage=1&pageSize=15",
            post_data=json.dumps({"lowerTime": lower, "upperTime": upper - 999}),
            all_headers=lambda: {"content-type": "application/json", "cookie": "secret"},
        )
        self.response = SimpleNamespace(url=self.request.url, request=self.request, ok=True)
        self.batches, self.counts, self.calls = batches, counts, []
        self.context = SimpleNamespace(request=SimpleNamespace(post=self.post))

    def wait_for_function(self, *_args, **_kwargs):
        pass

    def expect_response(self, predicate, **_kwargs):
        assert not predicate(SimpleNamespace(url=self.request.url, request=SimpleNamespace(post_data="{}")))
        assert predicate(self.response)
        response = self.response

        class Capture:
            def __enter__(self):
                return SimpleNamespace(value=response)

            def __exit__(self, *_args):
                return False

        return Capture()

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, copy.deepcopy(kwargs)))
        index = len(self.calls) - 1
        return SimpleNamespace(ok=True, json=lambda: {
            "data": {"rowCount": self.counts[index], "data": self.batches[index]}
        })


def fetch(source, monkeypatch, batches, counts):
    page = ApiPage(source, batches, counts)
    monkeypatch.setattr(source, "_set_day_filter", lambda *_args: ("start", "end"))
    return source._fetch_filtered_api_items(page, DAY), page


def test_api_collects_all_pages_with_exact_day_bounds_and_no_cookie_copy(source, monkeypatch):
    (_, _, items), page = fetch(source, monkeypatch, [[record(0), record(1)], [record(2)]], [3, 3])
    assert len(items) == 3
    assert len({item.source_key for item in items}) == 3
    assert [call[1]["params"]["currPage"] for call in page.calls] == [1, 2]
    assert all(call[1]["data"]["upperTime"] == 1793807999999 for call in page.calls)
    assert all("cookie" not in call[1]["headers"] for call in page.calls)


@pytest.mark.parametrize("batches,counts,message", [
    ([[record(0)], []], [3, 3], "拉取不完整"),
    ([[record(0), record(1)], [record(0)]], [3, 3], "重复记录"),
    ([[record(0), record(1)], [record(2)]], [3, 4], "总数在分页期间发生变化"),
])
def test_incomplete_duplicate_or_shifting_pages_are_rejected(source, monkeypatch, batches, counts, message):
    with pytest.raises(RuntimeError, match=message):
        fetch(source, monkeypatch, batches, counts)


def test_empty_day_returns_no_records_and_page_limit_is_enforced(source, monkeypatch):
    (_, _, items), _ = fetch(source, monkeypatch, [[]], [0])
    assert items == []
    source.source["pagination"] = {"max_pages": 1}
    with pytest.raises(RuntimeError, match="超过 max_pages"):
        fetch(source, monkeypatch, [[record(0)]], [3])


def test_review_replenishment_persistence_and_upload_stay_in_their_project(source, monkeypatch, tmp_path):
    from app import db
    from app.core.registry import build_sink
    from app.services.review_service import ReviewService

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(tmp_path / "data"))
    db.init_db()
    template = json.loads((Path(__file__).resolve().parents[1] / "config/config.example.json").read_text(encoding="utf-8"))
    station = source.source["station"]
    pid = f"binxin_billet_{station}"
    project = next(p for p in template["projects"] if p["id"] == pid)
    project["cache"]["result_export_dir"] = str(tmp_path / pid)
    calls = []

    def fetch_day(self, day):
        calls.append(day)
        return [self._api_record_to_item(record(i), day, 1, i + 1) for i in range(4)]

    def materialize(self, image_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"cached test image")
        return destination

    monkeypatch.setattr(type(source), "fetch_day", fetch_day)
    monkeypatch.setattr(type(source), "materialize_image", materialize)
    service = ReviewService()
    monkeypatch.setattr(service, "project_dict", lambda project_id: project if project_id == pid else None)
    tables = {}

    def sink_factory(config):
        sink = build_sink(config)

        def upload_day(day, summary):
            _, row = sink._local_result(day, summary)
            cfg = config["sink"]
            key = (cfg["document_url"], cfg["selectors"]["sheet_name"])
            table, replaced, _ = sink._build_expected_table(tables.get(key, []), row, day, cfg)
            tables[key] = table
            return {"uploaded": True, "sheet": key[1], "replaced": replaced}

        monkeypatch.setattr(sink, "upload_day", upload_day)
        return sink

    monkeypatch.setattr("app.services.review_service.build_sink", sink_factory)
    state = service.prepare(pid, DAY, 2)
    initial_ids = [entry["item_id"] for entry in state["entries"]]
    initial_queue_id = state["current"]["queue_id"]
    assert state["local_images_ready"] == 2
    assert [e["item_id"] for e in service.prepare(pid, DAY, 2)["entries"]] == initial_ids
    assert calls == [DAY]
    with pytest.raises(RuntimeError, match="审核尚未完成"):
        service.upload(pid, DAY)
    state = service.decide(pid, DAY, initial_queue_id, "invalid")
    assert state["queue_length"] == 3
    incorrect_id = state["current"]["queue_id"]
    state = service.decide(pid, DAY, incorrect_id, "incorrect")
    state = service.decide(pid, DAY, state["current"]["queue_id"], "correct")
    assert state["complete"] and state["valid_count"] == 2
    assert state["invalid"] == 1 and state["accuracy"] == 0.5
    result = service.upload(pid, DAY)
    assert result["uploaded"] and result["sheet"] == {"72": "1期-72", "74": "1期-74"}[station]
    assert service.state(pid, DAY)["uploaded"]
    # Correcting an invalid entry removes its excess replacement, keeping the denominator at 2.
    state = service.decide(pid, DAY, initial_queue_id, "correct")
    assert state["valid_count"] == 2 and state["queue_length"] == 2 and state["invalid"] == 0
    state = service.decide(pid, DAY, incorrect_id, "correct")
    assert state["accuracy"] == 1
    assert service.upload(pid, DAY)["replaced"] == 1
    assert len(next(iter(tables.values()))) == 2  # Header and one date, even after re-upload.
    saved = json.loads((tmp_path / pid / "2026-11-04_summary.json").read_text(encoding="utf-8"))
    assert saved["sample_count"] == 2 and saved["accuracy"] == 100
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM items WHERE project_id<>?", (pid,)).fetchone()[0] == 0
