from datetime import date
from types import SimpleNamespace

import pytest

from app import db
from app.core.models import SourceItem
from app.desktop.statistics_report import (HEADERS, StatisticsDialog, clipboard_formats,
                                           copy_statistics, daily_statistics, point_name)
from app.services.review_service import ReviewService


@pytest.fixture(autouse=True)
def release_clipboard(qapp):
    yield
    # The offscreen Windows clipboard must release its owned QMimeData before
    # Qt shuts down; the real desktop uses the Windows clipboard integration.
    qapp.clipboard().clear()


@pytest.fixture
def local_review(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'review.sqlite3')
    db.init_db()
    service = ReviewService()
    monkeypatch.setattr(service, 'project_dict', lambda pid: {
        'id': pid, 'daily_target': 2, 'source': {}, 'sink': {'enabled': False}})

    def prepare(pid, day, decisions):
        db.upsert_items(pid, day.isoformat(), [SourceItem(source_key=str(n), recognition_text='test')
                                              for n in range(10)])
        state = service.prepare(pid, day, 2)
        for decision in decisions:
            state = service.decide(pid, day, state['current']['queue_id'], decision)
        return state
    return service, prepare


def test_daily_table_counts_excludes_unfinished_and_keeps_requested_blank_columns(local_review):
    service, prepare = local_review
    day = date(2026, 2, 10)
    prepare('binxin_billet_72', day, ['invalid', 'other', 'correct', 'incorrect'])
    prepare('binxin_billet', day, ['correct', 'correct'])
    prepare('unfinished', day, ['correct'])
    prepare('binxin_billet_72', date(2026, 2, 11), ['incorrect', 'incorrect'])
    projects = [SimpleNamespace(id=pid, name='test') for pid in ('binxin_billet_72', 'binxin_billet')]
    rows, incomplete = daily_statistics(day, projects)
    assert incomplete == 1
    assert rows == [
        ['2026/2/10', '72', '4', '1', '1', '', '', '2', '50.00%'],
        ['2026/2/10', '73', '2', '2', '0', '', '', '0', '100.00%'],
    ]
    state = service.state('binxin_billet_72', day)
    assert state['other'] == state['invalid'] == 1  # Export never rewrites audit classifications.
    assert daily_statistics(date(2026, 1, 1), projects) == ([], 0)


def test_point_names_keep_distinct_phase_two_cameras():
    assert point_name(SimpleNamespace(id='binxin_billet_74', name='74/71')) == '74'
    assert point_name(SimpleNamespace(id='binxin_cc2_hot', name='2连-2棒线热送辊道')) == '2连-2棒线热送辊道'


def test_copy_uses_real_html_table_and_tsv_with_optional_headers(qapp):
    rows = [['2026/2/10', 'A<&>点位', '50', '49', '1', '', '', '0', '98.00%']]
    text, html = clipboard_formats(rows)
    assert text.split('\r\n')[0].split('\t') == list(HEADERS)
    assert text.split('\r\n')[1].split('\t') == rows[0]
    assert '<table ' in html and html.count('<th ') == 9
    assert 'A&lt;&amp;&gt;点位' in html and '<td style=' in html
    copy_statistics(rows, include_headers=False)
    mime = qapp.clipboard().mimeData()
    assert mime.hasHtml() and mime.hasText()
    assert mime.text().split('\t') == rows[0] and '<th ' not in mime.html()


def test_dialog_changes_date_and_disables_copy_for_empty_day(qapp, local_review):
    _, prepare = local_review
    day = date(2026, 2, 10)
    prepare('binxin_billet_72', day, ['correct', 'incorrect'])
    dialog = StatisticsDialog(day, [SimpleNamespace(id='binxin_billet_72', name='72/79')])
    dialog.show()
    assert dialog.rows[0][1] == '72' and dialog.copy_button.isEnabled()
    dialog.copy_button.click()
    assert qapp.clipboard().text().startswith('日期\t点位\t')
    assert '已复制 1 行' in dialog.feedback.text()
    dialog.include_headers.setChecked(False)
    dialog.copy()
    assert qapp.clipboard().text().startswith('2026/2/10\t72\t')
    dialog.set_day(date(2026, 2, 11))
    assert dialog.table.rowCount() == 0 and not dialog.copy_button.isEnabled()
    dialog.close()


def test_manual_mode_persists_and_blocks_even_forced_pending_sync(qapp, local_review, monkeypatch):
    from app.desktop.window import ReviewWindow
    service, prepare = local_review
    window = ReviewWindow(check_network=False)
    window.sync_timer.stop()
    window.cache_timer.stop()
    state = prepare(window.projects[0].id, date(2026, 2, 10), ['correct', 'incorrect'])
    window.sync.enqueue(state)
    assert window.sync.due() is not None
    window.manual_report.setChecked(True)
    monkeypatch.setattr(window.sync, 'due', lambda: pytest.fail('Manual mode must not poll the upload queue'))
    monkeypatch.setattr(window.service, 'upload', lambda *_: pytest.fail('No upload allowed in manual mode'))
    window._try_sync()
    window._try_sync(force=True)
    window.preferences.sync()
    assert window.preferences.value('completion_mode') == 'table'
    assert window.manual_report.isChecked() and not window.auto_upload.isChecked()
    assert window.auth_button.isHidden() and window.upload_button.text() == '查看统计表'
    window.close()
    restarted = ReviewWindow(check_network=False)
    assert restarted.manual_report.isChecked() and not restarted.auto_upload.isChecked()
    # Durable uploads remain recoverable if the user later selects automatic sync.
    assert restarted.sync.due() is not None
    restarted.auto_upload.setChecked(True)
    assert not restarted.manual_report.isChecked() and restarted.auth_button.isHidden() is False
    assert restarted.upload_button.text() == '立即同步 / 重试'
    restarted.close()


def test_completed_review_opens_daily_table_once_and_button_reopens(qapp, local_review, monkeypatch):
    from PySide6.QtGui import QImage
    from app.desktop.window import ReviewWindow
    _, prepare = local_review
    window = ReviewWindow(check_network=False)
    window.manual_report.setChecked(True)
    pid = window.projects[0].id
    day = date(2026, 2, 10)
    state = prepare(pid, day, ['correct', 'incorrect'])
    # A local image avoids invoking any source while showing the completed state.
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    window._apply_state((state, image))
    qapp.processEvents()
    dialog = window.statistics_dialog
    assert dialog.isVisible() and dialog.rows[0][0] == '2026/2/10'
    assert window.sync.due() is None
    dialog.close()
    window._apply_state((state, image))
    qapp.processEvents()
    assert not dialog.isVisible()  # Navigating reviewed images cannot keep reopening it.
    window.upload()
    assert dialog.isVisible()
    dialog.close()
    window.close()


def test_old_opt_out_is_migrated_to_table_mode(qapp, local_review):
    from PySide6.QtCore import QSettings
    from app.desktop.window import ReviewWindow
    prefs = QSettings(str(db.DB_PATH.parent / 'desktop-preferences.ini'), QSettings.Format.IniFormat)
    prefs.setValue('auto_upload', False)
    prefs.sync()
    window = ReviewWindow(check_network=False)
    assert window.manual_report.isChecked()
    window.close()
