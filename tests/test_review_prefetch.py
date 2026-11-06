from datetime import date
from pathlib import Path

from app import db
from app.core.config import settings
from app.core.models import SourceItem
from app.services.review_service import ReviewService


class _FakeSource:
    def __init__(self):
        self.fetch_count = 0
        self.materialize_calls: list[str] = []

    def fetch_day(self, business_date: date) -> list[SourceItem]:
        self.fetch_count += 1
        return [
            SourceItem(
                source_key=f"sample-{index}",
                recognition_text=f"TEXT-{index}",
                image_url=f"http://box/images/{index}.jpg",
                metadata={"timestamp": f"{business_date.isoformat()} 12:00:0{index}"},
            )
            for index in range(4)
        ]

    def materialize_image(self, image_url: str, destination: Path) -> Path:
        self.materialize_calls.append(image_url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake image")
        return destination


def test_prepare_prefetches_only_queue_and_replenishment_stays_on_demand(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    monkeypatch.setattr(settings.storage, "data_root", str(tmp_path / "data"))
    db.init_db()

    project = {
        "id": "edge-prefetch",
        "name": "edge-prefetch",
        "daily_target": 2,
        "source": {
            "type": "fake",
            "refresh_on_prepare": False,
            "prefetch_sample_images": True,
        },
        "cache": {},
        "sink": {"enabled": False},
        "diagnostics": {},
    }
    source = _FakeSource()
    service = ReviewService()
    monkeypatch.setattr(service, "project_dict", lambda _project_id: project)
    monkeypatch.setattr(
        "app.services.review_service.build_source", lambda _project: source
    )
    business_date = date(2026, 9, 4)

    state = service.prepare(project["id"], business_date)

    assert source.fetch_count == 1
    assert len(source.materialize_calls) == 2
    assert state["local_images_ready"] == 2
    assert state["local_images_downloaded"] == 2
    initial_ids = {entry["item_id"] for entry in state["entries"]}
    with db.connect() as conn:
        initial_rows = conn.execute(
            "SELECT id, image_path FROM items WHERE id IN (?, ?)", tuple(initial_ids)
        ).fetchall()
    assert all(Path(row["image_path"]).exists() for row in initial_rows)

    # Re-opening the same review session uses the local files and does no I/O.
    reopened = service.prepare(project["id"], business_date)
    assert source.fetch_count == 1
    assert len(source.materialize_calls) == 2
    assert reopened["local_images_ready"] == 2
    assert reopened["local_images_downloaded"] == 0

    # An invalid decision adds one replacement without prefetching it.  Its first
    # image request is the deliberate online replenishment point.
    state = service.decide(
        project["id"], business_date, state["current"]["queue_id"], "invalid"
    )
    replacement_ids = {entry["item_id"] for entry in state["entries"]} - initial_ids
    assert len(replacement_ids) == 1
    replacement_id = replacement_ids.pop()
    assert len(source.materialize_calls) == 2

    replacement_path = service.materialize_item_image(replacement_id)

    assert replacement_path.exists()
    assert len(source.materialize_calls) == 3


def test_metadata_refresh_preserves_an_existing_local_image(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    db.init_db()
    business_date = "2026-09-04"
    local_image = tmp_path / "cached.jpg"
    local_image.write_bytes(b"cached")

    db.upsert_items(
        "edge-prefetch",
        business_date,
        [
            SourceItem(
                source_key="sample-1",
                recognition_text="OLD",
                image_path=local_image,
                image_url="http://box/old.jpg",
            )
        ],
    )
    db.upsert_items(
        "edge-prefetch",
        business_date,
        [
            SourceItem(
                source_key="sample-1",
                recognition_text="NEW",
                image_url="http://box/new.jpg",
            )
        ],
    )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT image_path, recognition_text FROM items WHERE source_key='sample-1'"
        ).fetchone()
    assert row["image_path"] == str(local_image.resolve())
    assert row["recognition_text"] == "NEW"
