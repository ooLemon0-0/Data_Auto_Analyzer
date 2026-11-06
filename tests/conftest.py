import os

import pytest


@pytest.fixture(scope="session")
def qapp():
    # Qt on Windows cannot safely destroy and recreate QApplication between
    # test modules. Keep one instance alive for the entire GUI test session.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.desktop.theme import apply_theme
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    return app


@pytest.fixture(autouse=True)
def dispose_test_windows():
    yield
    import sys
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    # Hidden dialogs still own timers. Dispose them between tests so queued
    # callbacks cannot outlive monkeypatched services and their test windows.
    for widget in app.topLevelWidgets():
        if widget.parent() is None:
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
