from datetime import date
from io import BytesIO

from PIL import Image
import pytest

from app.core.cancellation import CancellationToken, WorkCancelled, cancellation_scope
from app.core.models import SourceItem
from app.desktop import collection
from app.sources.base import NoSourceRecords


def photo(color):
    buffer = BytesIO()
    Image.new("RGB", (80, 60), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def item(original=""):
    return SourceItem("record", "text", image_url="cropped", metadata={"original_image_url": original})


def test_collection_samples_inclusive_range_only_uses_originals_and_skips_failed_sample(tmp_path, monkeypatch):
    red, blue, green = (photo(color) for color in ("red", "blue", "green"))
    queried, downloaded = [], []

    class Source:
        def fetch_day(self, day):
            queried.append(day.day)
            if day.day == 21:
                raise NoSourceRecords()
            return ([item("broken"), item("red"), item()] if day.day == 20 else
                    [item("red"), item("blue"), item("green")])

        def materialize_image(self, url, destination):
            downloaded.append(url)
            destination.write_bytes({"red": red, "blue": blue, "green": green, "broken": b"bad"}[url])
            return destination

    monkeypatch.setattr(collection, "build_source", lambda _: Source())
    monkeypatch.setattr(collection.random, "shuffle", lambda candidates: candidates.reverse())
    result = collection.collect_originals({"id": "site"}, date(2026, 11, 20), date(2026, 11, 22), 5, tmp_path)
    assert queried == [20, 21, 22]
    assert downloaded == ["green", "blue", "red", "broken"]
    assert (result["saved"], result["available"], result["failed"]) == (3, 4, 1)
    images = sorted(result["directory"].iterdir())
    assert len(images) == 3 and all(image.suffix == ".jpg" for image in images)
    assert [image.read_bytes() for image in images] == [green, blue, red]
    assert "2026-11-22" in images[0].name and "2026-11-20" in images[-1].name


def test_collection_keeps_completed_images_when_stopped(tmp_path, monkeypatch):
    token = CancellationToken()
    red = photo("red")

    class Source:
        def fetch_day(self, _day):
            return [item("one"), item("two")]

        def materialize_image(self, url, destination):
            destination.write_bytes(red)
            if url == "two":
                token.cancel()
            return destination

    monkeypatch.setattr(collection, "build_source", lambda _: Source())
    monkeypatch.setattr(collection.random, "shuffle", lambda _: None)
    day = date(2026, 11, 20)
    with cancellation_scope(token), pytest.raises(WorkCancelled):
        collection.collect_originals({"id": "site"}, day, day, 2, tmp_path)
    output = list(next(tmp_path.iterdir()).iterdir())
    assert len(output) == 1 and output[0].read_bytes() == red
