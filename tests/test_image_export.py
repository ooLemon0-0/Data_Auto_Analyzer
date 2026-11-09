from pathlib import Path
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QStandardPaths, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from app import db
from app.core.config import settings
from app.desktop.image_export import save_review_image
from app.desktop.widgets import ImageViewer


def picture():
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor('#192A3B'))
    image.setPixelColor(20, 40, QColor('#2CB6B3'))  # end-face overlay pixel
    return image


def test_export_full_resolution_overlay_rotation_and_duplicate_names(tmp_path):
    image = picture()
    first = save_review_image(image, '../点位:*', '2026-11-09', '../../key?1', directory=tmp_path)
    second = save_review_image(image, '../点位:*', '2026-11-09', '../../key?1', directory=tmp_path)
    assert first.parent == second.parent == tmp_path and first != second
    assert second.stem.endswith('(2)') and first.exists()
    saved = QImage(str(first))
    assert saved.size() == image.size() and saved.pixelColor(20, 40) == image.pixelColor(20, 40)
    rotated = save_review_image(image, '点位', '2026-11-09', 'rotated', rotation=1, directory=tmp_path)
    checked = QImage(str(rotated))
    assert checked.width() == 360 and checked.height() == 640
    assert checked.pixelColor(319, 20) == image.pixelColor(20, 40)


def test_export_uses_redirected_system_downloads_and_rejects_empty_image(tmp_path, monkeypatch):
    redirected = tmp_path / 'moved downloads'
    def location(kind):
        assert kind == QStandardPaths.StandardLocation.DownloadLocation
        return str(redirected)
    monkeypatch.setattr(QStandardPaths, 'writableLocation', location)
    saved = save_review_image(picture(), '点位', '2026-11-09', 'key')
    assert saved.parent == redirected and saved.exists()
    with pytest.raises(ValueError):
        save_review_image(QImage(), 'p', '2026-11-09', 'invalid', directory=redirected)
    assert len(list(redirected.iterdir())) == 1


def test_hover_button_hides_without_image_remains_clickable_and_tracks_resize(qapp):
    viewer = ImageViewer()
    viewer.resize(800, 500)
    viewer.show()
    qapp.processEvents()
    QCoreApplication.sendEvent(viewer.viewport(), QEvent(QEvent.Type.Enter))
    assert not viewer.save_button.isVisible()
    viewer.set_image(picture())
    assert viewer.save_button.isVisible()
    point = viewer.save_button.mapToGlobal(viewer.save_button.rect().center())
    with patch('app.desktop.widgets.QCursor.pos', return_value=point):
        QCoreApplication.sendEvent(viewer.viewport(), QEvent(QEvent.Type.Leave))
    assert viewer.save_button.isVisible()
    calls = []
    viewer.save_requested.connect(lambda: calls.append(True))
    QTest.mouseClick(viewer.save_button, Qt.MouseButton.LeftButton)
    assert calls == [True]
    viewer.resize(1000, 500)
    qapp.processEvents()
    assert viewer.save_button.x() == viewer.viewport().width() - viewer.save_button.width() - 16
    with patch('app.desktop.widgets.QCursor.pos', return_value=QPoint(-100000, -100000)):
        QCoreApplication.sendEvent(viewer.viewport(), QEvent(QEvent.Type.Leave))
    assert not viewer.save_button.isVisible()
    viewer.set_image(None)
    QCoreApplication.sendEvent(viewer.viewport(), QEvent(QEvent.Type.Enter))
    assert not viewer.save_button.isVisible()
    viewer.close()


def test_desktop_export_snapshot_does_not_lock_review_or_change_audits(tmp_path, monkeypatch, qapp):
    from app.desktop.window import ReviewWindow
    from threading import Event
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'review.sqlite3')
    monkeypatch.setattr(settings.storage, 'data_root', str(tmp_path / 'cache'))
    window = ReviewWindow(check_network=False)
    window.auto_upload.setChecked(False)
    window.state = {'project_id': window.projects[0].id, 'business_date': '2026-11-09',
        'current': {'source_key': 'RAW/001', 'item_id': 999}}
    window.viewer.set_image(picture())
    window.show()
    before = db.DB_PATH.read_bytes()
    entered, release = Event(), Event()
    def save(image, project, day, key, **kwargs):
        entered.set()
        assert release.wait(5)
        return save_review_image(image, project, day, key, directory=tmp_path / 'Downloads', **kwargs)
    monkeypatch.setattr('app.desktop.window.save_review_image', save)
    try:
        window.viewer.save_requested.emit()
        assert entered.wait(3)
        assert not window.busy and window.fetch_button.isEnabled() and not window.viewer.save_button.isEnabled()
        window.viewer.set_image(QImage(4, 4, QImage.Format.Format_RGB32))
        release.set()
        deadline = time.monotonic() + 5
        while not window.viewer.save_button.isEnabled() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.01)
        assert window.viewer.save_button.isEnabled()
        saved = list((tmp_path / 'Downloads').glob('*.png'))
        assert len(saved) == 1 and QImage(str(saved[0])).size() == picture().size()
        assert str(saved[0]) in window.status.text() and window.notice is None
        assert db.DB_PATH.read_bytes() == before
    finally:
        release.set()
        window.close()
