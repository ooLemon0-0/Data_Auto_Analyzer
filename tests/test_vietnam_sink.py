from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.desktop.bootstrap import relocate_config
from app.projects.vietnam.sink import VietnamQingTuiSink


HEADERS = ["日期", "抽样数", "正确数量", "错误数量", "模糊、喷印差数量", "准确率"]


@pytest.fixture
def sink(tmp_path):
    config = json.loads(Path("config/config.example.json").read_text(encoding="utf-8"))
    project = next(p for p in config["projects"] if p["id"] == "vietnam_billet")
    relocated = relocate_config({"projects": [project]}, tmp_path, None)
    return VietnamQingTuiSink(relocated["projects"][0])


def test_vietnam_columns_preserve_existing_blank_cells_and_support_date_upsert(sink):
    cfg = sink.config["sink"]
    assert cfg["browser"]["remote_debugging_port"] == 19424
    assert Path(cfg["browser"]["user_data_dir"]).name == "vietnam_billet"
    day = date(2026, 11, 24)
    previous = ["2026/11/22", "50", "", "3", "", "0"]
    _, row = sink._local_result(day, {"sample_count": 50, "correct": 47, "incorrect": 3,
                                      "invalid": 2, "other": 9, "accuracy": 94.0})
    table, replaced, had_header = sink._build_expected_table([HEADERS, previous], row, day, cfg)
    assert had_header and replaced == 0
    assert table == [HEADERS, previous, ["2026/11/24", "50", "47", "3", "2", "0.94"]]
    _, updated = sink._local_result(day, {"sample_count": 50, "correct": 50, "incorrect": 0,
                                          "invalid": 0, "other": 9, "accuracy": 100.0})
    table, replaced, _ = sink._build_expected_table(table, updated, day, cfg)
    assert replaced == 1
    assert table[-1][1:5] == ["50", "50", "", ""]
    assert sink._semantic_table_mismatch(table, [*table[:-1], [*table[-1][:-1], "100.00%"]],
                                         sink._column_specs(cfg), cfg) is None
    with pytest.raises(RuntimeError, match="未写入"):
        sink._build_expected_table([HEADERS, previous + ["额外内容"]], row, day, cfg)


def test_vietnam_selects_unique_matching_sheet(sink, monkeypatch):
    names = ["其他表", "Sheet1"]
    clicked = []
    tabs = SimpleNamespace(count=lambda: len(names), nth=lambda i: SimpleNamespace(click=lambda: clicked.append(names[i])))
    monkeypatch.setattr(sink, "_find_wps_frame", lambda _p: SimpleNamespace(locator=lambda _s: tabs))
    tables = {"其他表": "日期\t其他列", "Sheet1": "\t".join(HEADERS)}
    monkeypatch.setattr(sink, "_copy_used_range", lambda _p, _s: tables[clicked[-1]])
    page = SimpleNamespace(wait_for_timeout=lambda _t: None)
    sink._select_sheet_if_configured(page, {})
    assert clicked == ["其他表", "Sheet1", "Sheet1"]
    tables["其他表"] = tables["Sheet1"]
    with pytest.raises(RuntimeError, match="唯一"):
        sink._select_sheet_if_configured(page, {})


def test_vietnam_open_editor_does_not_reuse_another_document(sink, monkeypatch):
    target = "https://web.qingtui.com/drive/preview/VN-exact"
    visited = []
    existing = SimpleNamespace(url=target + "-another", bring_to_front=lambda: pytest.fail("Wrong document reused"))
    page = SimpleNamespace(url="about:blank", goto=lambda url, **_: visited.append(url), bring_to_front=lambda: None)
    context = SimpleNamespace(pages=[existing], new_page=lambda: page)
    monkeypatch.setattr(sink, "_wait_for_auth_if_needed", lambda *_: None)
    monkeypatch.setattr(sink, "_wps_editor_ready", lambda _: True)
    assert sink._open_editor(context, target, {}, {"match_document_url": True}) is page
    assert visited == [target]
    page.url = target + "?view=normal"
    context.pages.append(page)
    assert sink._open_editor(context, target, {}, {"match_document_url": True}) is page
    assert visited == [target]
