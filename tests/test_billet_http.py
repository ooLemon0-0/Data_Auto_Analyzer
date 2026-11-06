from datetime import date
from types import SimpleNamespace

import pytest

from app.core.progress import progress_scope
from app.projects.binxin_73_84.source import Binxin7384HistorySource
from app.projects.binxin_72_79.source import Binxin7279HistorySource
from app.projects.binxin_74_71.source import Binxin7471HistorySource
from app.sources.billet_http import day_bounds, record_to_item


DAY = date(2026, 11, 4)


def record(index):
    return {"id": index, "accTime": day_bounds(DAY)[0] + index * 1000,
            "identifyPictureUrl": f"http://images/{index}.jpg", "identifyBilletNumber": f"ABC{index}"}


class Session:
    def __init__(self, batches=None, count=3, token="test-token"):
        self.headers, self.calls, self.closed = {}, [], False
        self.batches = batches or [[record(1), record(2)], [record(3)]]
        self.count, self.token = count, token

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/account/login"):
            return SimpleNamespace(headers={"token": self.token}, raise_for_status=lambda: None)
        index = kwargs["params"]["currPage"] - 1
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "success": True, "data": {"rowCount": self.count, "data": self.batches[index]}})

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


@pytest.fixture(params=[Binxin7384HistorySource, Binxin7279HistorySource, Binxin7471HistorySource])
def source(request, monkeypatch):
    source = request.param({"source": {"history_url": "http://site:3000/#/history", "api": {"page_size": 2}}})
    monkeypatch.setattr(source, "_ensure_easyconnect", lambda: None)
    monkeypatch.setattr(source, "_credential", lambda field: "authorized-" + field)
    # Opening a browser is a regression, even when an API or image request fails.
    monkeypatch.setattr(source, "_launch_context", lambda *_a: pytest.fail("Acquisition launched a browser"))
    return source


def test_all_first_phase_sources_fetch_exact_day_without_browser(source, monkeypatch):
    session = Session()
    monkeypatch.setattr("app.sources.billet_http.requests.Session", lambda: session)
    events = []
    with progress_scope(events.append):
        items = source.fetch_day(DAY)
    assert len(items) == 3 and session.closed
    assert session.headers["Authorization"] == "test-token"
    history = [call for call in session.calls if "/history/" in call[0]]
    assert [call[1]["params"]["currPage"] for call in history] == [1, 2]
    lower, upper = day_bounds(DAY)
    assert all(call[1]["json"] == {"lowerTime": lower, "upperTime": upper} for call in history)
    assert [(e["current"], e["total"]) for e in events if e["current"] is not None] == [(2, 3), (3, 3)]
    mapper = getattr(source, "_api_record_to_item", None)
    if mapper:
        assert items[0].source_key == mapper(record(1), DAY, 1, 1).source_key


@pytest.mark.parametrize("batches,count,message", [
    ([[record(1), record(2)], [record(1)]], 3, "重复"),
    ([[record(1)], []], 3, "不完整"),
    ([[{**record(1), "accTime": day_bounds(DAY)[1] + 1}]], 1, "日期之外"),
])
def test_http_transport_rejects_corrupt_or_cross_day_data(monkeypatch, batches, count, message):
    source = Binxin7384HistorySource({"source": {"history_url": "http://site/#/history", "api": {"page_size": 2}}})
    monkeypatch.setattr(source, "_ensure_easyconnect", lambda: None)
    monkeypatch.setattr(source, "_credential", lambda _field: "built-in")
    session = Session(batches, count)
    monkeypatch.setattr("app.sources.billet_http.requests.Session", lambda: session)
    with pytest.raises(RuntimeError, match=message):
        source.fetch_day(DAY)
    assert session.closed


def test_login_failure_closes_session_without_chrome_fallback(source, monkeypatch):
    session = Session(token="")
    monkeypatch.setattr("app.sources.billet_http.requests.Session", lambda: session)
    with pytest.raises(RuntimeError, match="无法登录"):
        source.fetch_day(DAY)
    assert session.closed


def test_last_millisecond_and_existing_source_identity_are_preserved():
    item = record_to_item({}, {**record(1), "accTime": day_bounds(DAY)[1]}, DAY, 1, 1)
    assert item.metadata["timestamp"] == "2026/11/04 23:59:59"


def test_image_download_failure_does_not_start_browser(source, monkeypatch, tmp_path):
    import requests
    def fail(*_args, **_kwargs):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr("app.sources.billet_http.requests.get", fail)
    with pytest.raises(requests.ConnectionError):
        source.materialize_image("http://image/pic.jpg", tmp_path / "pic.jpg")
