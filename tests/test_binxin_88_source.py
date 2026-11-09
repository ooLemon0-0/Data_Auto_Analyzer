import copy
from datetime import date
import json
from pathlib import Path

import pytest
from PIL import Image

from app import db
from app.core.config import settings
from app.core.registry import build_sink, build_source
from app.desktop.statistics_report import daily_statistics
from app.desktop.tasks import project_category
from app.projects.binxin_88.source import Binxin88HistorySource
from app.services.review_service import ReviewService


DAY = date(2026, 11, 8)
CAMERAS = [dict(id=f'camera{i}', cameraName=f'OCR_2#_辊道_相机{i}',
                businessGroupId='group88', ocrCamera=True) for i in (1, 2)]


@pytest.fixture
def project(tmp_path):
    config = json.loads(Path('config/config.example.json').read_text(encoding='utf-8'))
    project = next(p for p in config['projects'] if p['id'] == 'binxin_billet_88')
    project['cache']['result_export_dir'] = str(tmp_path / 'results')
    project['source']['api']['page_size'] = 2
    return project


@pytest.fixture
def source(project):
    return build_source(project)


def record(index, camera=1):
    return {'id': f'record{index}', 'accTime': 1794096000000 + index * 1000,
            'identifyFullUrl': f'http://images/camera{camera}_frame_origin_{index}.jpg',
            'identifyPictureUrl': f'http://images/camera{camera}_frame_crop_{index}.jpg',
            'identifyCoordinate': [10, 10, 70, 60], 'identifyBilletNumber': ''}


def resolve(source, monkeypatch, cameras=CAMERAS):
    monkeypatch.setattr(source, '_query', lambda *_args, **_kwargs: {'data': cameras})
    return source._resolve_camera(None)


def test_registration_category_sheet_and_camera_origin_without_explicit_camera_id(source, project, monkeypatch):
    assert isinstance(source, Binxin88HistorySource)
    assert project_category(settings.project('binxin_billet_88')) == '镔鑫一期'
    assert project['sink']['selectors']['sheet_name'] == '1期-88'
    assert project['sink']['selectors']['create_sheet_if_missing'] is True
    camera = resolve(source, monkeypatch)
    items = [source._record_to_item(record(i, i), camera, DAY) for i in (1, 2)]
    assert [item.metadata['camera_id'] for item in items] == ['camera1', 'camera2']
    assert [item.metadata['camera_name'] for item in items] == [c['cameraName'] for c in CAMERAS]
    assert all(item.image_url == item.metadata['original_image_url'] for item in items)
    assert all(item.recognition_text == '' and item.metadata['review_image_kind'] == 'boxed' for item in items)
    assert len({item.source_key for item in items}) == 2


@pytest.mark.parametrize('cameras', [[], CAMERAS + [CAMERAS[0]],
    [CAMERAS[0], {**CAMERAS[1], 'ocrCamera': False}],
    [CAMERAS[0], {**CAMERAS[1], 'businessGroupId': 'unrelated'}]])
def test_incomplete_ambiguous_non_ocr_or_mixed_groups_are_rejected(source, monkeypatch, cameras):
    with pytest.raises(RuntimeError):
        resolve(source, monkeypatch, cameras)


def test_unknown_or_conflicting_image_camera_is_rejected(source, monkeypatch):
    camera = resolve(source, monkeypatch)
    with pytest.raises(RuntimeError, match='无法确定相机来源'):
        source._record_to_item({**record(1), 'identifyFullUrl': 'http://images/unknown.jpg',
                              'identifyPictureUrl': ''}, camera, DAY)
    with pytest.raises(RuntimeError, match='相机来源不一致'):
        source._record_to_item({**record(1), 'identifyPictureUrl': 'http://images/camera2_crop.jpg'}, camera, DAY)
    with pytest.raises(RuntimeError, match='相机来源不一致'):
        source._record_to_item({**record(1), 'cameraId': 'camera2'}, camera, DAY)
    with pytest.raises(RuntimeError, match='日期之外'):
        source._record_to_item({**record(1), 'accTime': 1}, camera, DAY)


def test_two_cameras_are_fetched_once_per_group_with_exact_day_bounds(source, monkeypatch):
    camera = resolve(source, monkeypatch)
    calls = []
    pages = [[record(1, 1), record(2, 2)], [record(3, 1)]]
    def query(_session, _endpoint, *, body, params):
        calls.append(copy.deepcopy(body))
        return {'rowCount': 3, 'data': pages[params['currPage'] - 1]}
    monkeypatch.setattr(source, '_query', query)
    items = source._fetch_camera_day(None, camera, DAY)
    assert len(items) == 3 and len(calls) == 2
    lower, upper = source._day_bounds_ms(DAY)
    assert calls == [{'lowerTime': lower, 'upperTime': upper, 'businessGroupId': 'group88'}] * 2


def test_review_export_and_sink_keep_88_isolated_and_replenish_other(project, tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'review.sqlite3')
    monkeypatch.setattr(settings.storage, 'data_root', str(tmp_path / 'data'))
    db.init_db()
    source = build_source(project)
    camera = resolve(source, monkeypatch)
    items = [source._record_to_item(record(i, 1 + i % 2), camera, DAY) for i in range(8)]
    monkeypatch.setattr(Binxin88HistorySource, 'fetch_day', lambda _self, _day: items)
    def image(_self, _url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (100, 80), '#334455').save(destination, format='JPEG')
        return destination
    monkeypatch.setattr(Binxin88HistorySource, 'materialize_image', image)
    service = ReviewService()
    monkeypatch.setattr(service, 'project_dict', lambda pid: project if pid == project['id'] else None)
    state = service.prepare(project['id'], DAY, 2)
    for decision in ('other', 'invalid', 'correct', 'incorrect'):
        state = service.decide(project['id'], DAY, state['current']['queue_id'], decision)
    assert state['complete'] and state['accuracy'] == .5 and state['other'] == state['invalid'] == 1
    rows, _ = daily_statistics(DAY, settings.projects)
    assert rows == [['2026/11/8', '88', '4', '1', '1', '', '', '2', '50.00%']]
    sink = build_sink(project)
    table = []
    def upload(day, summary):
        nonlocal table
        _, row = sink._local_result(day, summary)
        table, _, _ = sink._build_expected_table(table, row, day, project['sink'])
        return {'uploaded': True}
    monkeypatch.setattr('app.services.review_service.build_sink', lambda _p: type('Sink', (), {'upload_day': staticmethod(upload)})())
    service.upload(project['id'], DAY)
    service.upload(project['id'], DAY)
    assert len(table) == 2 and table[-1][-1] == '1'
    with db.connect() as conn:
        assert {r[0] for r in conn.execute('SELECT project_id FROM review_sessions')} == {'binxin_billet_88'}
