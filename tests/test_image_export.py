from pathlib import Path
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QStandardPaths, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from app import db
from app.core.config import settings
from app.desktop.image_export import save_original_image
from app.desktop.widgets import ImageViewer


def picture():
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor('#192A3B'))
    image.setPixelColor(20, 40, QColor('#2CB6B3'))  # end-face overlay pixel
    return image


def original(tmp_path):
    from PIL import Image
    path = tmp_path / 'full.jpg'
    Image.new('RGB',(960,540),'#192A3B').save(path)
    return path


def test_export_original_bytes_and_duplicate_names(tmp_path):
    source = original(tmp_path)
    first = save_original_image(source, '../点位:*', '2026-11-09', '../../key?1', directory=tmp_path / 'downloads')
    second = save_original_image(source, '../点位:*', '2026-11-09', '../../key?1', directory=tmp_path / 'downloads')
    assert first.parent == second.parent == tmp_path / 'downloads' and first != second
    assert first.suffix == '.jpg' and second.stem.endswith('(2)')
    assert first.read_bytes() == second.read_bytes() == source.read_bytes()


def test_export_uses_redirected_system_downloads(tmp_path, monkeypatch):
    redirected = tmp_path / 'moved downloads'
    monkeypatch.setattr(QStandardPaths, 'writableLocation', lambda _kind: str(redirected))
    source = original(tmp_path)
    saved = save_original_image(source,'点位','2026-11-09','key')
    assert saved.parent == redirected and saved.read_bytes() == source.read_bytes()


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


def test_desktop_confirmation_saves_raw_not_display_and_keeps_review_responsive(tmp_path,monkeypatch,qapp):
    from app.desktop.window import ReviewWindow
    from threading import Event
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'review.sqlite3')
    monkeypatch.setattr(settings.storage,'data_root',str(tmp_path/'cache'))
    monkeypatch.setattr(QStandardPaths,'writableLocation',lambda _kind:str(tmp_path/'Downloads'))
    window=ReviewWindow(check_network=False)
    window.auto_upload.setChecked(False)
    window.state={'project_id':window.projects[0].id,'business_date':'2026-11-09','current':{'source_key':'RAW/001','item_id':999}}
    window.viewer.set_image(picture());window.viewer.rotate();window.viewer.change_zoom(2)
    window.show()
    before=db.DB_PATH.read_bytes()
    source=original(tmp_path)
    entered,release=Event(),Event()
    def materialize(item_id):
        assert item_id==999
        entered.set();assert release.wait(5)
        return source
    monkeypatch.setattr(window.service,'materialize_original_image',materialize)
    try:
        window.viewer.save_requested.emit()
        assert window.save_confirmation.isVisible() and not entered.is_set()
        window.save_confirmation.cancel_button.click()
        assert not entered.is_set() and not (tmp_path/'Downloads').exists()
        window.viewer.save_requested.emit()
        window.save_confirmation.confirm_button.click()
        assert entered.wait(3)
        assert not window.busy and window.fetch_button.isEnabled() and not window.viewer.save_button.isEnabled()
        window.viewer.set_image(QImage(4,4,QImage.Format.Format_RGB32))
        release.set()
        deadline=time.monotonic()+5
        while not window.viewer.save_button.isEnabled() and time.monotonic()<deadline:
            qapp.processEvents();time.sleep(.01)
        saved=list((tmp_path/'Downloads').glob('*.jpg'))
        assert len(saved)==1 and saved[0].read_bytes()==source.read_bytes()
        assert str(saved[0]) in window.status.text() and window.notice is None
        assert db.DB_PATH.read_bytes()==before
    finally:
        release.set();window.close()
