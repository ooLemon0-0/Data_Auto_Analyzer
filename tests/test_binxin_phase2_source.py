import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.registry import build_source
from app.projects.binxin_phase2.source import BinxinPhase2HistorySource


DAY = date(2026, 11, 4)
CAMERA = {"id": "wanted", "cameraName": "目标点位", "businessGroupId": "group-a", "ocrCamera": True}


@pytest.fixture
def source():
    return BinxinPhase2HistorySource({"source": {
        "type": "binxin_phase2_history", "base_url": "http://box:3000",
        "station": "76", "point_name": "目标点位", "api": {"page_size": 2},
        "auth": {"username": "test-user", "password": "test-password"},
    }})


def record(index=0, camera_id="wanted"):
    return {"id": index, "accTime": 1793764800000 + index * 1000,
            "cameraId": camera_id, "identifyFullUrl": f"http://images/{index}.jpg",
            "identifyPictureUrl": f"http://images/{index}_crop.jpg", "identifyBilletNumber": ""}


def test_api_record_keeps_empty_ocr_and_isolates_same_box_cameras(source):
    item = source._record_to_item(record(), CAMERA, DAY)
    assert item.source_key == "76_wanted_0"
    assert item.recognition_text == ""
    assert item.image_url == "http://images/0.jpg"
    assert item.metadata["point_name"] == "目标点位"
    assert item.metadata["business_group_id"] == "group-a"
    assert source._record_to_item(record(camera_id="another-point"), CAMERA, DAY) is None
    with pytest.raises(RuntimeError, match="相机标识"):
        source._record_to_item(record(camera_id=""), CAMERA, DAY)


def test_calendar_uses_china_time_and_validates_every_record(source):
    assert source._day_bounds_ms(DAY) == (1793721600000, 1793807999999)
    with pytest.raises(RuntimeError, match="指定日期之外"):
        source._record_to_item({**record(), "accTime": 1793808000000}, CAMERA, DAY)


@pytest.mark.parametrize("cameras,message", [
    ([], "未唯一匹配"),
    ([CAMERA, CAMERA], "未唯一匹配"),
    ([{**CAMERA, "businessGroupId": ""}], "业务分组"),
    ([{**CAMERA, "ocrCamera": False}], "未配置为 OCR"),
])
def test_camera_resolution_rejects_ambiguous_or_non_ocr_configuration(source, monkeypatch, cameras, message):
    monkeypatch.setattr(source, "_query", lambda *_a, **_k: {"data": cameras})
    with pytest.raises(RuntimeError, match=message):
        source._resolve_camera(None)


def test_complete_group_is_collected_before_camera_filtering(source, monkeypatch):
    calls = []
    pages = [[record(0), record(1, "other")], [record(2)]]

    def query(session, endpoint, *, body, params):
        calls.append((copy.deepcopy(body), params))
        return {"rowCount": 3, "data": pages[params["currPage"] - 1]}

    monkeypatch.setattr(source, "_query", query)
    items = source._fetch_camera_day(None, CAMERA, DAY)
    assert [item.metadata["record_id"] for item in items] == [0, 2]
    assert len(calls) == 2
    assert all(body == {"lowerTime": 1793721600000, "upperTime": 1793807999999,
                        "businessGroupId": "group-a"} for body, _ in calls)


@pytest.mark.parametrize("pages,counts,message", [
    ([[record(0)], []], [3, 3], "不完整"),
    ([[record(0), record(1)], [record(0)]], [3, 3], "重复记录"),
    ([[record(0), record(1)], [record(2)]], [3, 4], "总数在分页过程中发生变化"),
])
def test_invalid_paginated_result_is_never_accepted(source, monkeypatch, pages, counts, message):
    def query(_session, _endpoint, *, body, params):
        index = params["currPage"] - 1
        return {"rowCount": counts[index], "data": pages[index]}
    monkeypatch.setattr(source, "_query", query)
    with pytest.raises(RuntimeError, match=message):
        source._fetch_camera_day(None, CAMERA, DAY)


def test_login_uses_ocr_backend_routing_header_and_tries_configured_candidates(source, monkeypatch):
    sessions = []

    class Session:
        def __init__(self):
            self.headers, self.logins, self.closed = {}, [], False
            sessions.append(self)

        def get(self, url, *, params, timeout):
            self.logins.append((url, params))
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"success": len(self.logins) == 2})

        def close(self):
            self.closed = True

    source.source["auth"]["candidates"] = [{"username": "fallback", "password": "fallback-password"}]
    monkeypatch.setattr("app.projects.binxin_phase2.source.requests.Session", Session)
    session = source._session()
    assert session.headers["x-server-origin"] == "http://127.0.0.1:18899"
    assert len(session.logins) == 2 and not session.closed
    assert session.logins[1][1]["account"] == "fallback"


def test_all_eleven_points_are_independent_and_use_the_confirmed_hosts():
    config = json.loads((Path(__file__).resolve().parents[1] / "config/config.example.json").read_text(encoding="utf-8"))
    points = [p for p in config["projects"] if p["source"]["type"] == "binxin_phase2_history"]
    assert len(points) == 11
    assert len({p["id"] for p in points}) == 11
    assert len({p["sink"]["selectors"]["sheet_name"] for p in points}) == 11
    assert len({p["cache"]["result_export_dir"] for p in points}) == 11
    by_id = {p["id"]: p for p in points}
    assert by_id["binxin_cc2_cold_entry"]["source"]["station"] == "76"
    assert by_id["binxin_cc5_hot_crane"]["source"]["station"] == "69"
    for point in points:
        assert type(build_source(point)) is BinxinPhase2HistorySource
        assert point["enabled"] and not point["diagnostics"]["enabled"]
        assert point["daily_target"] == 50
        assert point["sink"]["selectors"]["create_sheet_if_missing"]
        assert point["sink"]["selectors"]["sheet_name"] == point["source"]["point_name"]
        assert len(point["sink"]["selectors"]["sheet_name"]) <= 31
