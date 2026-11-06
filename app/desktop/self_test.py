from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import sys
import traceback
import uuid
import time
from threading import Event


def run_self_test(output: Path) -> int:
    report = {"ok": False, "checks": {}}
    report["frozen"] = bool(getattr(sys, "frozen", False))
    try:
        from PIL import Image
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QSettings, qVersion
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication
        from app.desktop.theme import apply_theme
        from app.desktop.widgets import ImageViewer, ProgressDialog, StartupDialog, NoticeDialog
        from playwright.sync_api import sync_playwright
        from app import db
        from app.core.config import settings
        from app.core.models import SourceItem
        from app.core.registry import build_source
        from app.desktop.bootstrap import VERSION, data_directory
        from app.desktop.sync import DesktopSync
        from app.services.review_service import ReviewService

        test_dir = data_directory() / "self-test" / uuid.uuid4().hex
        test_dir.mkdir(parents=True)
        db.DB_PATH = test_dir / "review.sqlite3"
        settings.storage.data_root = str(test_dir / "images")
        db.init_db()
        report["version"] = VERSION
        report["checks"]["embedded_config"] = len(settings.projects)
        for project in settings.projects:
            if project.enabled:
                build_source(project.model_dump())
        report["checks"]["registered_sources"] = True
        app = QApplication.instance() or QApplication([])
        apply_theme(app)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(test_dir / "preferences"))
        report["checks"]["native_qt"] = qVersion()
        sample = test_dir / "sample.jpg"
        Image.new("RGB", (120, 80), "#16345d").save(sample)
        from app.projects.binxin_phase2.review_image import render_review
        rendered, overlay = render_review(sample, test_dir / "review.png", {
            "original_image_url": "self-test", "review_box": [20, 25, 90, 65]}, "LOCAL")
        with Image.open(rendered) as checked:
            assert checked.getpixel((20, 45)) == (44, 182, 179)
        assert overlay["review_image_kind"] == "boxed"
        report["checks"]["phase2_review_overlay"] = True
        viewer = ImageViewer()
        viewer.resize(800, 400)
        viewer.set_image(QImage(str(sample)))
        viewer.rotate()
        viewer.change_zoom(1.2)
        assert viewer.rotation == 1 and viewer.zoom == 1.2
        progress = ProgressDialog()
        progress.update_progress({"stage": "images", "message": "self-test", "current": 27, "total": 50})
        assert progress.progress.value() == 27 and progress.progress.maximum() == 50
        progress.reject()
        assert progress.active  # Esc cannot dismiss a running job.
        progress.finish()
        report["checks"]["native_image_and_modal_progress"] = True
        startup = StartupDialog()
        startup.show()
        app.processEvents()
        startup.finish()
        assert not startup.isVisible()
        report["checks"]["startup_transition"] = True
        day, project_id = date(2026, 1, 1), "desktop-self-test"
        db.upsert_items(project_id, day.isoformat(), [
            SourceItem(source_key=f"test-{i}", recognition_text=f"TEST-{i}", image_path=sample)
            for i in range(4)
        ])
        service = ReviewService()
        service.project_dict = lambda _pid: {"id": project_id, "daily_target": 2, "source": {}, "sink": {"enabled": False}}
        state = service.prepare(project_id, day, 2)
        state = service.decide(project_id, day, state["current"]["queue_id"], "invalid")
        assert state["queue_length"] == 3
        state = service.decide(project_id, day, state["current"]["queue_id"], "correct")
        state = service.decide(project_id, day, state["current"]["queue_id"], "incorrect")
        assert state["complete"] and state["accuracy"] == 0.5
        assert service.materialize_item_image(state["current"]["item_id"]).exists()
        report["checks"]["local_review_replenishment"] = True
        queue = DesktopSync(service)
        assert queue.enqueue(state) and not queue.enqueue(state)
        # Only the transport is replaced: no self-test rows are sent to the real document.
        service.upload = lambda *_args: {"uploaded": True}
        assert queue.run(queue.due())["uploaded"]
        assert queue.due() is None
        report["checks"]["durable_sync_queue"] = True
        from app.desktop.window import ReviewWindow
        window = ReviewWindow(check_network=False)
        assert Path(window.preferences.fileName()).is_relative_to(test_dir)
        assert window.auto_upload.isChecked(), "新用户应默认自动同步"
        report["checks"]["isolated_preferences"] = True
        window.auto_upload.setChecked(False)
        window.resize(1280, 800)
        window.show()
        app.processEvents()
        assert window.fetch_button.isVisible() and window.viewer.width() > 600
        window.grab().save(str(test_dir / "workspace.png"))
        # Exercise the shipped scheduler/UI while replacing only remote data
        # acquisition. All records and preferences stay inside the self-test DB.
        from app.desktop.batch_dialogs import ProjectSelectionDialog
        from app.desktop.downloads import task_key
        first, second = window.projects[:2]
        selection = ProjectSelectionDialog(window, window.projects, selected=[first.id, second.id], target=2)
        assert selection.start_button.isEnabled() and len(selection.selected_ids()) == 2
        selection.reject()
        original_prepare = window.service.prepare
        window.service.project_dict = lambda pid: {"id": pid, "daily_target": 2, "source": {}, "sink": {"enabled": False}}
        release = Event()
        def local_prepare(pid, day, target):
            if pid == second.id:
                assert release.wait(8), "后台任务未收到测试完成信号"
            db.upsert_items(pid, str(day), [SourceItem(source_key=f"batch-{pid}-{i}", recognition_text=f"LOCAL{i}", image_path=sample) for i in range(4)])
            return original_prepare(pid, day, target)
        window.service.prepare = local_prepare
        window.downloads.ensure_route = lambda _p: None
        def pump_until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert predicate(), "桌面并发流程测试超时"
        try:
            window.start_downloads([first.id, second.id], day, 2)
            pump_until(lambda: window.state is not None and not window.busy)
            assert window.state["project_id"] == first.id
            assert window.downloads.get(task_key(second.id, day))["status"] == "running"
            window.decide("correct")
            pump_until(lambda: not window.busy)
            assert window.state["correct"] == 1
            window.show_download_progress()
            assert window.download_dialog.isVisible() and not window.download_dialog.isModal()
            window.download_dialog.close()
            assert not window.download_dialog.isVisible() and window.downloads.has_active()
            release.set()
            pump_until(lambda: not window.downloads.has_active())
            app.processEvents()
            assert window.state["project_id"] == first.id
            window._open_task(task_key(second.id, day))
            pump_until(lambda: not window.busy)
            assert window.state["project_id"] == second.id and window.state["valid_count"] == 0
            report["checks"]["batch_parallel_review"] = True
            report["checks"]["progress_hide_and_reopen"] = True
            window.grab().save(str(test_dir / "batch_workspace.png"))
            from app.core.cancellation import cancel_pause
            started = Event()
            def cancellable_prepare(*_a):
                started.set()
                cancel_pause(30)
            window.service.prepare = cancellable_prepare
            cancelled_day = date(2026, 1, 2)
            window.start_downloads([first.id], cancelled_day, 2)
            pump_until(started.is_set)
            cancelled_key = task_key(first.id, cancelled_day)
            window.downloads.cancel(cancelled_key)
            pump_until(lambda: not window.downloads.has_active())
            assert window.downloads.get(cancelled_key)["status"] == "cancelled"
            assert window.service.state(first.id, day)["correct"] == 1
            assert (test_dir / "download_tasks.json").exists()
            report["checks"]["cancel_download_preserves_saved_review"] = True
        finally:
            release.set()
            pump_until(lambda: not window.downloads.has_active() and not window.busy)
        window.close()
        report["checks"]["desktop_workspace"] = True
        browser_config = next(p.sink["browser"] for p in settings.projects if p.sink.get("enabled"))
        executable = browser_config.get("executable_path")
        assert executable and Path(executable).exists(), "内置浏览器不存在"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page()
            page.goto("data:text/html,<title>AutoAnalyzer Self Test</title><h1>OK</h1>")
            assert page.title() == "AutoAnalyzer Self Test"
            report["checks"]["bundled_browser"] = browser.version
            browser.close()
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
