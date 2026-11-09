"""Portable Qt application; initialize settings in a worker behind the startup card."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time

from app.desktop.bootstrap import data_directory, initialize


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", type=Path, metavar="REPORT_JSON")
    parser.add_argument("--ui-smoke-test", type=Path, metavar="REPORT_JSON", help=argparse.SUPPRESS)
    parser.add_argument("--exit-smoke-test", type=Path, metavar="REPORT_JSON", help=argparse.SUPPRESS)
    parser.add_argument("--update-smoke-test", type=Path, metavar="REPORT_JSON", help=argparse.SUPPRESS)
    parser.add_argument("--required-update-smoke-test", type=Path, metavar="REPORT_JSON", help=argparse.SUPPRESS)
    args = parser.parse_args()
    test_report = args.self_test or args.ui_smoke_test or args.exit_smoke_test or args.update_smoke_test or args.required_update_smoke_test
    if test_report:
        os.environ["AUTOANALYZER_DATA_DIR"] = str(test_report.resolve().parent / ("." + test_report.stem + "-data"))
    initialize()
    if args.self_test:
        from app.desktop.self_test import run_self_test
        return run_self_test(args.self_test)
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication
    from app.desktop.theme import apply_theme
    from app.desktop.widgets import NoticeDialog, StartupDialog
    from app.desktop.startup import notify_window_visible, report_startup, set_startup_callback

    app = QApplication(sys.argv[:1])
    apply_theme(app)
    app.setApplicationName("数据抽样审核")
    mutex = None
    if sys.platform == "win32":
        ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
        key = hashlib.sha256(str(data_directory()).encode()).hexdigest()[:16]
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AutoAnalyzer_" + key)
        if ctypes.windll.kernel32.GetLastError() == 183:
            notify_window_visible()
            NoticeDialog(None, "程序已经打开", "请从 Windows 任务栏切换到已有的审核窗口。").exec()
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
            return 0
    splash = StartupDialog()
    splash.show()
    app.processEvents()
    notify_window_visible()
    cancelled = Event()
    def cancel_startup():
        cancelled.set()
        set_startup_callback(None)
        if hasattr(app, "update_dialog"):
            app.update_dialog.finished = True
            app.update_dialog.token.cancel()
        app.quit()
    splash.exit_requested.connect(cancel_startup)
    if args.ui_smoke_test:
        args.ui_smoke_test.parent.mkdir(parents=True, exist_ok=True)
        app.processEvents()
        splash.grab().save(str(args.ui_smoke_test.with_suffix(".startup.png")))

    class StartupSignals(QObject):
        ready = Signal(object)
        restored = Signal(object)
        status = Signal(str, object, object)
    signals = StartupSignals()
    signals.status.connect(splash.set_stage)
    set_startup_callback(signals.status.emit)

    def loaded(error):
        if cancelled.is_set():
            return
        if error:
            splash.hide()
            NoticeDialog(None, "无法启动程序", "请重新打开程序。如果问题持续，请将详细信息提供给维护人员。",
                         detail=f"{error}\n\n诊断文件：{data_directory() / 'logs/desktop.log'}").exec()
            app.quit()
            return
        from app.desktop.window import ReviewWindow
        try:
            splash.set_stage("创建审核界面")
            app.processEvents()
            app.window = ReviewWindow(check_network=False, defer_restore=True)
            def restore():
                try:
                    report_startup("恢复本地审核任务")
                    app.window.downloads.restore_sessions()
                    signals.restored.emit(None)
                except Exception as exc:
                    signals.restored.emit(str(exc))
            Thread(target=restore, daemon=True).start()
        except Exception as exc:
            logging.exception("Desktop startup failed")
            signals.restored.emit(str(exc))

    def workspace_ready(error):
        if cancelled.is_set():
            return
        if error:
            splash.hide()
            NoticeDialog(None, "无法读取本地任务", "请重新打开程序。", detail=error).exec()
            app.quit()
            return
        try:
            set_startup_callback(None)
            app.window.finish_startup(check_network=not bool(test_report))
            app.window.show()
            splash.finish()
            if args.update_smoke_test:
                def update_capture():
                    from app.desktop.bootstrap import resource_root
                    report = {"ok": app.window.isVisible() and app.update_dialog.finished,
                              "frozen": bool(getattr(sys, "frozen", False)),
                              "skipped": app.update_dialog.token.cancelled,
                              "installer_bundled": (resource_root() / "app/desktop/assets/install-update.ps1").is_file()}
                    args.update_smoke_test.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    app.window.close()
                QTimer.singleShot(500, update_capture)
            elif args.ui_smoke_test:
                def capture():
                    app.window.grab().save(str(args.ui_smoke_test.with_suffix(".workspace.png")))
                    report = {"ok": app.window.isVisible() and not splash.isVisible(),
                              "frozen": bool(getattr(sys, "frozen", False)),
                              "qt_platform": app.platformName(), "startup_hidden": not splash.isVisible()}
                    args.ui_smoke_test.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    app.window.close()
                QTimer.singleShot(500, capture)
            elif args.exit_smoke_test:
                from datetime import date
                args.exit_smoke_test.parent.mkdir(parents=True, exist_ok=True)
                entered = Event()
                def unresponsive(*_args):
                    entered.set()
                    Event().wait(90)
                app.window.auto_upload.setChecked(False)
                app.window.service.prepare = unresponsive
                app.window.downloads.ensure_route = lambda _p: None
                app.window.start_downloads([app.window.projects[0].id], date(2026, 11, 4), 2)
                def exit_capture():
                    if not entered.is_set():
                        QTimer.singleShot(50, exit_capture)
                        return
                    app.window.close()
                    app.processEvents()
                    app.window.exit_dialog.grab().save(str(args.exit_smoke_test.with_suffix(".dialog.png")))
                    start = time.monotonic()
                    app.window.exit_dialog.exit_button.click()
                    report = {"ok": app.window.closed and not app.window.isVisible(),
                              "frozen": bool(getattr(sys, "frozen", False)), "unresponsive_task_seconds": 90,
                              "close_request_ms": round((time.monotonic()-start)*1000),
                              "tasks": [j["status"] for j in app.window.downloads.snapshots()]}
                    args.exit_smoke_test.write_text(json.dumps(report, indent=2), encoding="utf-8")
                QTimer.singleShot(100, exit_capture)
        except Exception as exc:
            logging.exception("Desktop startup failed")
            splash.hide()
            NoticeDialog(None, "无法启动程序", "请重新打开程序。", detail=str(exc)).exec()
            app.quit()
    signals.ready.connect(loaded)
    signals.restored.connect(workspace_ready)

    def prepare():
        try:
            # Config extraction and service imports can take time on the first run.
            # Never block painting or animation in the startup window.
            from app.core.config import settings
            from app.desktop.window import ReviewWindow
            report_startup("检查本地数据库")
            from app import db
            db.init_db()
            signals.ready.emit(None)
        except Exception as exc:
            logging.exception("Desktop initialization failed")
            signals.ready.emit(str(exc))
    def begin_startup():
        splash.skip_button.hide()
        splash.show()
        splash.set_stage("加载审核组件")
        QTimer.singleShot(50, lambda: Thread(target=prepare, daemon=True).start())
    if (getattr(sys, "frozen", False) and not test_report) or args.update_smoke_test or args.required_update_smoke_test:
        from app.desktop.update_dialog import UpdateDialog
        if args.update_smoke_test:
            from app.desktop.updates import ASSET_NAME, REPOSITORY, Release
            from app.desktop.bootstrap import VERSION
            import app.desktop.update_dialog as update_module
            def unavailable(_token):
                from app.desktop.updates import UpdateUnavailable
                raise UpdateUnavailable('离线检查验证：更新服务无法连接。')
            update_module.check_for_update = unavailable
        if args.required_update_smoke_test:
            from app.desktop.updates import Release, REPOSITORY
            import app.desktop.update_dialog as update_module
            release_download = Event()
            update_module.check_for_update = lambda _t: Release('9.0.0', f'https://github.com/{REPOSITORY}/releases/download/v9.0.0/AutoAnalyzer.exe', '0'*64, 100)
            def required_download(*_args):
                if not release_download.wait(10): raise RuntimeError('Update gate test did not reach capture')
                return args.required_update_smoke_test.parent / 'fixture-update.exe'
            update_module.download_update = required_download
            def installed(*_args):
                result = {'ok': not hasattr(app,'window') and getattr(app,'required_skip_blocked',False),
                    'frozen':bool(getattr(sys,'frozen',False)), 'mandatory':True, 'installer_called':True,
                    'workbench_created':hasattr(app,'window')}
                args.required_update_smoke_test.write_text(json.dumps(result,indent=2),encoding='utf-8')
            update_module.launch_installer = installed
        app.update_dialog = UpdateDialog()
        splash.set_stage("检查更新")
        splash.skip_button.hide()
        app.update_dialog.update_available.connect(splash.hide)
        app.update_dialog.continue_startup.connect(begin_startup)
        app.update_dialog.install_started.connect(app.quit)
        app.update_dialog.exit_requested.connect(cancel_startup)
        app.update_dialog.begin(show=False)
        if args.required_update_smoke_test:
            args.required_update_smoke_test.parent.mkdir(parents=True,exist_ok=True)
            def capture_required():
                if app.update_dialog.state != 'downloading':
                    QTimer.singleShot(50,capture_required); return
                app.update_dialog.skip()
                app.required_skip_blocked = not app.update_dialog.finished and not app.update_dialog.skip_button.isVisible()
                app.update_dialog.grab().save(str(args.required_update_smoke_test.with_suffix('.dialog.png')))
                release_download.set()
            QTimer.singleShot(100,capture_required)
        if args.update_smoke_test:
            args.update_smoke_test.parent.mkdir(parents=True, exist_ok=True)
            def capture_update():
                if app.update_dialog.state != 'offline':
                    QTimer.singleShot(50, capture_update)
                    return
                app.update_dialog.grab().save(str(args.update_smoke_test.with_suffix(".dialog.png")))
                app.update_dialog.skip_button.click()
            QTimer.singleShot(100, capture_update)
    else:
        QTimer.singleShot(50, lambda: Thread(target=prepare, daemon=True).start())
    result = app.exec()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
