from types import SimpleNamespace

from PySide6.QtCore import QSettings

from app.desktop import window


def test_announcement_opens_three_times_per_version_and_manual_use_does_not_count(tmp_path, qapp, monkeypatch):
    path = str(tmp_path / "preferences.ini")
    preferences = lambda: QSettings(path, QSettings.Format.IniFormat)
    shown = []
    owner = SimpleNamespace(preferences=preferences(), show_update_announcement=lambda: shown.append("open"))
    monkeypatch.setattr(window, "VERSION", "8.0.1")
    for _ in range(4):
        owner.preferences = preferences()
        window.ReviewWindow.show_startup_announcement(owner)
    assert len(shown) == 3
    owner.show_update_announcement()
    assert preferences().value("update_announcements/8.0.1", type=int) == 3
    monkeypatch.setattr(window, "VERSION", "8.0.2")
    owner.preferences = preferences()
    window.ReviewWindow.show_startup_announcement(owner)
    assert len(shown) == 5
    assert preferences().value("update_announcements/8.0.2", type=int) == 1
