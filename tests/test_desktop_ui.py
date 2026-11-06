import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import db
from app.desktop.theme import apply_theme
from app.desktop.widgets import ProgressDialog


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    return app


def test_modal_progress_cannot_be_dismissed_while_downloading(app):
    dialog = ProgressDialog()
    dialog.show()
    dialog.update_progress({"stage": "images", "message": "已缓存 27 / 50 张图片", "current": 27, "total": 50})
    app.processEvents()
    assert dialog.isModal() and dialog.progress.value() == 27
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    dialog.close()
    assert dialog.isVisible() and dialog.active
    dialog.finish()
    assert not dialog.isVisible()


def test_progress_waits_indeterminately_until_transport_reports_a_count(app):
    dialog = ProgressDialog()
    dialog.update_progress({"stage": "records", "message": "正在等待接口"})
    assert dialog.progress.minimum() == dialog.progress.maximum() == 0
    dialog.update_progress({"stage": "records", "message": "已读取", "current": 500, "total": 750})
    assert dialog.progress.value() == 500 and dialog.progress.maximum() == 750
    dialog.finish()


def test_worker_modal_stays_responsive_and_failure_unlocks_workspace(app, tmp_path, monkeypatch):
    from app.desktop.window import ReviewWindow
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "preferences"))
    window = ReviewWindow(check_network=False)
    assert window.auto_upload.isChecked()
    assert window.preferences.fileName().replace("\\", "/").startswith(tmp_path.as_posix())
    window.auto_upload.setChecked(False)
    window.show()
    notices = []
    monkeypatch.setattr(window, "_notice", lambda *a, **k: notices.append((a, k)))
    from threading import Event
    release = Event()
    def work():
        assert release.wait(5)
        raise RuntimeError("connection timed out")
    window._run(work, lambda _r: pytest.fail("Failed operation returned success"), "正在拉取", modal="正在准备审核图片")
    app.processEvents()
    assert window.busy and window.progress_dialog.isVisible() and not window.fetch_button.isEnabled()
    window.close()
    assert window.isVisible()  # Closing the parent cannot interrupt the worker.
    release.set()
    deadline = time.monotonic() + 5
    while window.busy and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert not window.busy and window.progress_dialog is None and window.fetch_button.isEnabled()
    assert notices and "内网" in notices[0][0][1]
    window.close()


def test_review_buttons_save_replenish_and_enable_upload_after_completion(app, tmp_path, monkeypatch):
    from datetime import date
    from PIL import Image
    from app.core.models import SourceItem
    from app.desktop.window import ReviewWindow
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "preferences"))
    window = ReviewWindow(check_network=False)
    window.auto_upload.setChecked(False)
    project = window._current_project()
    local = {"id": project.id, "daily_target": 2, "source": {}, "sink": {"enabled": False}}
    monkeypatch.setattr(window.service, "project_dict", lambda _pid: local)
    sample = tmp_path / "sample.jpg"
    Image.new("RGB", (120, 80), "#445566").save(sample)
    day = date(2026, 11, 4)
    db.upsert_items(project.id, day.isoformat(), [SourceItem(source_key=f"sample-{i}", image_path=sample,
        recognition_text=f"TEST{i}") for i in range(4)])
    state = window.service.prepare(project.id, day, 2)
    window._apply_state(window._state_with_image(state))
    window.show()
    def click(index):
        QTest.mouseClick(window.decision_buttons[index], Qt.MouseButton.LeftButton)
        deadline = time.monotonic() + 5
        while window.busy and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert not window.busy
    click(2)
    assert window.state["invalid"] == 1 and window.state["queue_length"] == 3
    click(0)
    click(1)
    assert window.state["complete"] and window.state["accuracy"] == 0.5
    assert window.upload_button.isEnabled() and window.sync.due() is None
    window.close()


def test_batch_selection_first_ready_review_and_progress_hide_reopen(app, tmp_path, monkeypatch):
    from datetime import date
    from threading import Event
    from PIL import Image
    from PySide6.QtWidgets import QComboBox
    from app.core.models import SourceItem
    from app.desktop.downloads import task_key
    from app.desktop.window import ReviewWindow
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    window = ReviewWindow(check_network=False)
    window.auto_upload.setChecked(False)
    window.show()
    assert not window.findChildren(QComboBox)
    first, slow = window.projects[:2]
    image = tmp_path / "photo.jpg"
    Image.new("RGB", (160, 100), "#667788").save(image)
    release = Event()
    original_prepare = window.service.prepare
    monkeypatch.setattr(window.service, "project_dict", lambda pid: {"id": pid, "daily_target": 2,
        "source": {}, "sink": {"enabled": False}})
    def prepare(pid, day, target):
        if pid == slow.id:
            assert release.wait(8)
        db.upsert_items(pid, str(day), [SourceItem(source_key=f"{pid}-{i}", image_path=image,
                                                recognition_text=f"LOCAL{i}") for i in range(4)])
        return original_prepare(pid, day, target)
    monkeypatch.setattr(window.service, "prepare", prepare)
    day = date(2026, 11, 4)
    def wait(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert predicate()
    try:
        window.prepare()
        chooser = window.selection_dialog
        assert chooser.isModal() and not chooser.start_button.isEnabled()
        chooser.checks[first.id].setChecked(True)
        chooser.checks[slow.id].setChecked(True)
        chooser.target.setValue(2)
        chooser.day.setDate(chooser.day.date().fromString(str(day), "yyyy-MM-dd"))
        QTest.mouseClick(chooser.start_button, Qt.MouseButton.LeftButton)
        wait(lambda: window.state is not None and not window.busy)
        assert window.state["project_id"] == first.id
        assert window.downloads.get(task_key(slow.id, day))["status"] == "running"
        assert not window.download_dialog.isVisible()
        QTest.mouseClick(window.decision_buttons[0], Qt.MouseButton.LeftButton)
        wait(lambda: not window.busy)
        assert window.state["correct"] == 1
        QTest.mouseClick(window.download_status_button, Qt.MouseButton.LeftButton)
        assert window.download_dialog.isVisible() and not window.download_dialog.isModal()
        window.download_dialog.close()
        assert not window.download_dialog.isVisible() and window.downloads.has_active()
        release.set()
        wait(lambda: not window.downloads.has_active())
        app.processEvents()
        assert window.state["project_id"] == first.id and window.state["correct"] == 1
        window._open_task(task_key(slow.id, day))
        wait(lambda: not window.busy)
        assert window.state["project_id"] == slow.id and window.state["valid_count"] == 0
        assert window.service.state(first.id, day)["correct"] == 1
    finally:
        release.set()
        wait(lambda: not window.downloads.has_active() and not window.busy)
        window.close()


def test_progress_separates_record_count_images_current_batch_and_stop_controls(app):
    from app.desktop.batch_dialogs import DownloadProgressDialog
    dialog = DownloadProgressDialog(None)
    old = {"key": "old", "name": "以前的任务", "project_id": "old", "business_date": "2026-11-03", "target": 50,
           "status": "ready", "stage": "ready", "message": "本地可审核", "error": "", "images_ready": 50, "images_total": 50}
    current = {**old, "key": "today", "name": "本批次点位", "business_date": "2026-11-04", "status": "running",
               "stage": "records", "records_current": 500, "records_total": 635, "images_ready": 0, "images_total": None,
               "message": "正在读取当天记录"}
    dialog.update_job(old)
    dialog.set_scope(["today"], "2026-11-04", 50)
    dialog.update_job(current)
    row = dialog.rows["today"]
    assert "500 / 635" in row["records"].text() and "目标 50" in row["images"].text()
    assert "0 / 1" in dialog.summary.text() and dialog.rows["old"]["frame"].isHidden()
    assert "尚未代表图片" in row["progress_caption"].text()
    dialog.update_job({**current, "stage": "images", "images_ready": 7, "images_total": 50, "message": "已缓存 7 张"})
    assert "7 / 50" in row["images"].text() and row["progress"].maximum() == 50
    stopped, resumed = [], []
    dialog.stop.connect(stopped.append)
    dialog.retry.connect(resumed.append)
    row["stop"].click()
    assert stopped == ["today"]
    dialog.update_job({**current, "status": "cancelled", "message": "已停止", "images_ready": 7, "images_total": 50})
    assert row["resume"].isHidden() is False and row["stop"].isHidden()
    row["resume"].click()
    assert resumed == ["today"]
    dialog.close()


def test_close_allows_stop_and_exit_with_download_running(app, tmp_path, monkeypatch):
    from datetime import date
    from threading import Event
    from app.core.cancellation import cancel_pause
    from app.desktop.window import ReviewWindow
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.sqlite3")
    window = ReviewWindow(check_network=False)
    window.auto_upload.setChecked(False)
    window.show()
    entered = Event()
    def prepare(*_a):
        entered.set()
        cancel_pause(30)
    monkeypatch.setattr(window.service, "prepare", prepare)
    window.start_downloads([window.projects[0].id], date(2026, 11, 4), 2)
    assert entered.wait(3)
    window.close()
    assert window.isVisible() and window.exit_dialog.isVisible()
    QTest.mouseClick(window.exit_dialog.exit_button, Qt.MouseButton.LeftButton)
    assert window.closed and not window.isVisible()
    deadline = time.monotonic()+3
    while window.downloads.has_active() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert not window.downloads.has_active()
