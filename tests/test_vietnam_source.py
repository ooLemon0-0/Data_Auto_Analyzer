from datetime import date, datetime
from hashlib import md5
from io import BytesIO

from PIL import Image
import pytest
import requests

from app.projects.vietnam import source as vietnam


HOST = "http://127.0.0.1:23180"
STORAGE = "http://192.0.2.18:19000"
OBJECT = "/ocr-identification/2026-11-24/algo-active/camera123_log_1234567890000_origin.jpg"


@pytest.fixture
def source():
    return vietnam.VietnamHistorySource({"source": {"base_url": HOST, "storage_origin": STORAGE,
                                                       "camera_name": "camera", "auth": {}}})


def test_vpn_absent_returns_guidance_without_accessing_site(source, monkeypatch):
    monkeypatch.setattr(vietnam, "running_processes", lambda: set())
    monkeypatch.setattr(source, "_session", lambda: pytest.fail("VPN must be checked before opening the site"))
    status = source.network_status()
    assert status["available"] is False
    assert vietnam.NETWORK_MESSAGE in status["message"]
    with pytest.raises(vietnam.NetworkUnavailable, match="GlobalProtect"):
        source.fetch_day(date(2026, 11, 24))


def test_missing_camera_id_uses_image_name_and_preserves_beijing_date(source):
    capture = datetime(2026, 11, 24, 0, 10, tzinfo=vietnam.BEIJING)
    ms = int(capture.timestamp() * 1000)
    row = {"id": 55, "accTime": ms, "identifyFullUrl": STORAGE + OBJECT, "identifyBilletNumber": "26A7871"}
    item = source._item(row, {"id": "camera123"}, ms - 10, ms + 10)
    assert item.metadata["timestamp"] == "2026/11/24 00:10:00"
    assert item.metadata["original_image_url"] == STORAGE + OBJECT
    assert source._item(row, {"id": "another_camera"}, ms - 10, ms + 10) is None
    with pytest.raises(RuntimeError, match="相机标识不一致"):
        source._item(dict(row, cameraId="another_camera"), {"id": "camera123"}, ms - 10, ms + 10)


def test_corrupt_equal_length_download_retries_and_only_keeps_verified_image(source, monkeypatch, tmp_path):
    with BytesIO() as buffer:
        Image.new("RGB", (16, 16), "red").save(buffer, format="JPEG")
        expected = buffer.getvalue()
    altered = bytes([expected[0] ^ 1]) + expected[1:]
    calls = []

    class Response:
        status_code = 200
        headers = {"Content-Length": str(len(expected)), "ETag": '"' + md5(expected, usedforsecurity=False).hexdigest() + '"'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield altered if len(calls) == 1 else expected

    class Session(Response):
        def get(self, url, **kwargs):
            calls.append(url)
            return Response()

    monkeypatch.setattr(vietnam, "running_processes", lambda: {"pangpa.exe"})
    monkeypatch.setattr(vietnam, "cancel_pause", lambda _: None)
    monkeypatch.setattr(source, "_session", Session)
    output = tmp_path / "sample.jpg"
    assert source.materialize_image(STORAGE + OBJECT, output) == output
    assert calls == [HOST + "/oss" + OBJECT + "?origin=" + STORAGE] * 2
    assert output.read_bytes() == expected
    assert list(tmp_path.iterdir()) == [output]
    with pytest.raises(ValueError, match="图片地址"):
        source.materialize_image("http://192.0.2.99" + OBJECT, output)


def test_expired_session_reauthenticates_only_once(source, monkeypatch):
    requests_seen = []
    replies = iter([{"code": "401", "success": False}, {"success": True, "data": {"rowCount": 2}}])
    monkeypatch.setattr(source, "_request_json", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(source, "_login", requests_seen.append)
    with requests.Session() as session:
        assert source._query(session, "/api/v1/history/condition-query") == {"rowCount": 2}
        assert requests_seen == [session]
