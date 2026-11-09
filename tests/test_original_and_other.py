from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app import db
from app.core.config import settings
from app.core.models import SourceItem
from app.desktop.sync import fingerprint
from app.services.review_service import ReviewService
from app.sinks.qingtui_document import QingTuiDocumentSink


@pytest.fixture
def service(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'review.sqlite3')
    monkeypatch.setattr(settings.storage,'data_root',str(tmp_path/'data'))
    db.init_db()
    service=ReviewService()
    monkeypatch.setattr(service,'project_dict',lambda _pid:{'id':'p','daily_target':1,'source':{},'sink':{'enabled':True}})
    return service


def test_other_is_separate_excluded_replenished_persisted_and_uploaded(service,monkeypatch,tmp_path):
    day=date(2026,11,9)
    db.upsert_items('p',str(day),[SourceItem(source_key=str(n),recognition_text='x') for n in range(5)])
    state=service.prepare('p',day,1)
    other_id=state['current']['queue_id']
    state=service.decide('p',day,other_id,'other')
    assert state['other']==1 and state['invalid']==0 and state['valid_count']==0
    assert state['accuracy'] is None and not state['complete'] and state['queue_length']==2
    state=service.decide('p',day,state['current']['queue_id'],'correct')
    assert state['complete'] and state['valid_count']==1 and state['accuracy']==1 and state['other']==1
    before=fingerprint(state)
    summaries=[]
    monkeypatch.setattr('app.services.review_service.build_sink',lambda _p:SimpleNamespace(upload_day=lambda _d,s:(summaries.append(s),{'uploaded':True})[-1]))
    service.upload('p',day)
    assert summaries[0]['other']==1 and summaries[0]['sample_count']==1 and summaries[0]['accuracy']==100
    state=service.decide('p',day,other_id,'invalid')
    assert state['other']==0 and state['invalid']==1 and state['valid_count']==1
    assert fingerprint(state)!=before
    with db.connect() as conn:assert conn.execute('SELECT decision FROM review_queue WHERE id=?',(other_id,)).fetchone()[0]=='invalid'


def test_raw_export_fetches_full_url_instead_of_crop_and_overlay(service,monkeypatch,tmp_path):
    from app.desktop.image_export import save_original_image
    raw=tmp_path/'full.jpg';crop=tmp_path/'crop.jpg';overlay=tmp_path/'overlay.png'
    Image.new('RGB',(1000,700),'#334455').save(raw)
    Image.new('RGB',(40,30),'red').save(crop)
    Image.new('RGB',(1000,700),'green').save(overlay)
    full_url='http://example.invalid/full.jpg'
    db.upsert_items('p','2026-11-09',[SourceItem(source_key='one',recognition_text='x',image_path=overlay,image_url='http://example.invalid/crop.jpg',
        metadata={'original_image_url':full_url,'original_cache_path':str(crop),'review_image_kind':'boxed'})])
    with db.connect() as conn:item=conn.execute('SELECT * FROM items').fetchone()
    calls=[]
    def download(url,destination):
        calls.append(url);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(raw.read_bytes());return destination
    monkeypatch.setattr('app.services.review_service.build_source',lambda _p:SimpleNamespace(materialize_image=download))
    path=service.materialize_original_image(item['id'])
    saved=save_original_image(path,'p','2026-11-09','one',directory=tmp_path/'Downloads')
    assert calls==[full_url] and saved.read_bytes()==raw.read_bytes()
    assert Image.open(saved).size==(1000,700)
    assert service.materialize_original_image(item['id'])==path and len(calls)==1
    with db.connect() as conn:assert conn.execute('SELECT image_path FROM items WHERE id=?',(item['id'],)).fetchone()[0]==str(overlay.resolve())


def test_missing_full_source_never_exports_crop_or_invented_raw_cache(service,monkeypatch,tmp_path):
    crop=tmp_path/'crop.jpg';Image.new('RGB',(32,32)).save(crop)
    db.upsert_items('p','2026-11-09',[SourceItem(source_key='one',recognition_text='x',image_path=crop,image_url='http://example.invalid/crop.jpg',metadata={'original_cache_path':str(crop)})])
    monkeypatch.setattr('app.services.review_service.build_source',lambda _p:SimpleNamespace(fetch_day=lambda _d:[]))
    with db.connect() as conn:item_id=conn.execute('SELECT id FROM items').fetchone()[0]
    with pytest.raises(RuntimeError,match='完整原图'):service.materialize_original_image(item_id)


def test_phase_two_reuses_only_full_url_hashed_raw_frame(service,monkeypatch,tmp_path):
    url='http://example.invalid/full.png'
    raw=Path(settings.storage.data_root)/'p/2026-11-09/images/originals'/(hashlib.sha256(url.encode()).hexdigest()+'.png')
    raw.parent.mkdir(parents=True);Image.new('RGB',(800,500)).save(raw)
    db.upsert_items('p','2026-11-09',[SourceItem(source_key='one',recognition_text='x',metadata={'original_image_url':url})])
    with db.connect() as conn:item_id=conn.execute('SELECT id FROM items').fetchone()[0]
    monkeypatch.setattr('app.services.review_service.build_source',lambda _p:pytest.fail('raw frame should be reused'))
    assert service.materialize_original_image(item_id)==raw


def test_sheet_other_column_appends_without_shifting_existing_data(tmp_path):
    config={'sink':{'format':{'columns':[{'field':key,'header':key} for key in ('date','sample_count','correct','incorrect','invalid','accuracy')],
        'include_other':True}},'cache':{'result_export_dir':str(tmp_path)}}
    sink=QingTuiDocumentSink(config)
    headers=['date','sample_count','correct','incorrect','invalid','accuracy']
    table=[headers,['2026-11-08','1','1','0','0','100']]
    expected,_,_=sink._build_expected_table(table,{'date':'2026-11-09','sample_count':1,'correct':1,'incorrect':0,'invalid':0,'accuracy':100,'other':2},date(2026,11,9),config['sink'])
    assert expected[0]==headers+['其他'] and expected[1][-1]=='0' and expected[2][-1]=='2'
