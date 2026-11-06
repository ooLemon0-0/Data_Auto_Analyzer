from datetime import date
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import db
from app.core.config import settings
from app.core.models import SourceItem
from app.desktop.tasks import TaskQuery, filter_tasks, project_category, task_rows, review_summaries
from app.desktop.downloads import DownloadManager
from app.services.review_service import ReviewService


def point(pid, kind="", name="", category=""):
    return SimpleNamespace(id=pid, name=name or pid, source={"type": kind}, category=category)


def test_categories_and_combined_search_date_review_status_sort():
    projects = {p.id: p for p in [point("binxin_a"), point("binxin_cc2", "binxin_phase2_history"),
        point("wuhu_a", name="芜湖相机"), point("ruifeng_a"), point("custom", category="指定分类")]}
    assert [project_category(p) for p in projects.values()] == ["镔鑫一期", "镔鑫二期", "芜湖", "瑞丰", "指定分类"]
    jobs = [{"key": f"{p}:{d}", "project_id": p, "name": projects[p].name, "business_date": d,
             "status": status, "target": 50} for p, d, status in [
        ("binxin_a", "2026-11-03", "ready"), ("binxin_a", "2026-11-04", "paused"),
        ("binxin_cc2", "2026-11-04", "ready"), ("wuhu_a", "2026-11-03", "cancelled"),
        ("ruifeng_a", "2026-11-02", "ready")]]
    reviews = {"binxin_a:2026-11-04": {"valid": 50, "target": 50, "complete": True, "uploaded": True}}
    rows = task_rows(jobs, projects, reviews)
    assert [r["business_date"] for r in filter_tasks(rows, TaskQuery(category="镔鑫一期"))] == ["2026-11-04", "2026-11-03"]
    assert [r["business_date"] for r in filter_tasks(rows, TaskQuery(category="镔鑫一期", sort="date_old"))] == ["2026-11-03", "2026-11-04"]
    found = filter_tasks(rows, TaskQuery(search="binxin 11-04", category="镔鑫一期", start="2026-11-04", end="2026-11-04", status="synced"))
    assert len(found) == 1 and found[0]["status"] == "paused"  # Audit/sync state survives missing images.
    assert len(filter_tasks(rows, TaskQuery(status="unfinished"))) == 4
    assert len(filter_tasks(rows, TaskQuery(category="芜湖", status="cancelled"))) == 1


def test_restore_includes_history_beyond_forty_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    db.init_db()
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    service = ReviewService()
    monkeypatch.setattr(service, "project_dict", lambda _p: {"id": "p", "daily_target": 1, "source": {}})
    for number in range(1, 46):
        day = date.fromordinal(date(2026, 1, 1).toordinal() + number)
        db.upsert_items("p", str(day), [SourceItem(source_key=str(number), recognition_text=str(number), image_path=image)])
        service.prepare("p", day, 1)
    manager = DownloadManager([point("p")], lambda _j: None, service=service)
    try:
        assert len(manager.restore_sessions()) == 45
    finally:
        manager.shutdown()


def test_recent_sort_uses_saved_activity_instead_of_restore_order(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    db.init_db()
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"cached")
    service = ReviewService()
    monkeypatch.setattr(service, "project_dict", lambda _p: {"id": "p", "daily_target": 1, "source": {}})
    for day in (date(2026, 1, 1), date(2026, 1, 2)):
        db.upsert_items("p", str(day), [SourceItem(source_key=str(day), recognition_text="sample", image_path=image)])
        service.prepare("p", day, 1)
    with db.connect() as conn:
        conn.execute("UPDATE review_sessions SET updated_at='2026-01-06T08:00:00' WHERE business_date='2026-01-01'")
        conn.execute("UPDATE review_sessions SET updated_at='2026-01-05T08:00:00' WHERE business_date='2026-01-02'")
    project = point("p")
    manager = DownloadManager([project], lambda _j: None, service=service)
    try:
        manager.restore_sessions()
        rows = task_rows(manager.snapshots(), {"p": project}, review_summaries())
        assert [r["business_date"] for r in filter_tasks(rows, TaskQuery(sort="recent"))] == ["2026-01-01", "2026-01-02"]
    finally:
        manager.shutdown()


def test_task_dialog_query_applies_and_cache_dialog_requires_explicit_confirmation(tmp_path, qapp):
    from app.desktop.task_dialog import TaskBrowserDialog
    from app.desktop.cache_dialog import CacheDialog
    from app.desktop.cache_store import CacheStore
    from app.desktop.theme import apply_theme
    app = qapp
    jobs = [{"key": "p:2026-11-04", "project_id": "p", "name": "测试点位", "business_date": "2026-11-04", "status": "paused", "target": 50}]
    rows = task_rows(jobs, {"p": point("p", category="镔鑫一期")}, {})
    browser = TaskBrowserDialog(None, TaskQuery(), rows)
    browser.show()
    browser.category.setCurrentIndex(browser.category.findData("镔鑫一期"))
    browser.search.setText("测试 11-04")
    assert browser.table.rowCount() == 1
    applied = []
    browser.query_applied.connect(applied.append)
    browser._apply()
    assert applied[0].category == "镔鑫一期" and applied[0].search == "测试 11-04"
    image = tmp_path / "data/p/2026-11-04/images/1.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"picture")
    dialog = CacheDialog(None, CacheStore(tmp_path / "data"), lambda: set())
    requested = []
    dialog.clear_requested.connect(requested.append)
    dialog.show()
    dialog.refresh()
    deadline = time.monotonic() + 3
    while dialog.loading and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert dialog.table.rowCount() == 1 and not dialog.loading
    dialog.select_all()
    dialog.clear_button.click()
    assert image.exists() and not requested and dialog.confirmation.isVisible()
    dialog.confirmation.confirm_button.click()
    assert requested == [["2026-11-04"]] and image.exists()  # Window owns the actual cleanup.
    dialog.hide()
