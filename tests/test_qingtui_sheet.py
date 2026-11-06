import json
from pathlib import Path
from types import SimpleNamespace

from app.sinks.qingtui_document import QingTuiDocumentSink


class _Sheet:
    def __init__(self, name: str):
        self.name = name
        self.clicked = False

    def get_attribute(self, key: str):
        return self.name if key == "data-name" else None

    def inner_text(self):
        return self.name

    def click(self, **_kwargs):
        self.clicked = True


class _Sheets:
    def __init__(self, values: list[_Sheet]):
        self.values = values

    def count(self):
        return len(self.values)

    def nth(self, index: int):
        return self.values[index]


class _Frame:
    def __init__(self, sheets: list[_Sheet]):
        self.sheets = sheets

    def locator(self, selector: str):
        assert selector == ".sheet-name"
        return _Sheets(self.sheets)


class _Page:
    def wait_for_timeout(self, _milliseconds: int):
        pass


def test_create_sheet_renames_the_new_tab_even_when_inserted_between_existing_tabs():
    class Sheet(_Sheet):
        def dblclick(self):
            frame.editing = self

    class AddButton:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            frame.sheets.insert(1, Sheet("Sheet3"))

    class RenameInput:
        def count(self):
            return 1

        def is_visible(self):
            return frame.editing is not None

        def fill(self, name):
            frame.editing.name = name

        def press(self, key):
            assert key == "Enter"

    class Frame:
        def __init__(self):
            self.sheets = [Sheet("existing-left"), Sheet("existing-right")]
            self.editing = None

        def locator(self, selector):
            if selector == ".sheet-name":
                return _Sheets(self.sheets)
            if selector == ".sheet-add":
                return SimpleNamespace(first=AddButton())
            if selector == "input.sheet-name-input":
                return SimpleNamespace(last=RenameInput())
            raise AssertionError(selector)

    frame = Frame()
    sink = QingTuiDocumentSink({})
    result = sink._create_sheet(_Page(), frame, "new-point", {
        "sheet_add_button": ".sheet-add", "sheet_rename_input": "input.sheet-name-input"
    })
    assert result.name == "new-point"
    assert [sheet.name for sheet in frame.sheets] == ["existing-left", "new-point", "existing-right"]


class _TestSink(QingTuiDocumentSink):
    def __init__(self, frame: _Frame):
        super().__init__({})
        self.frame = frame
        self.created = None

    def _find_wps_frame(self, _page):
        return self.frame

    def _create_sheet(self, _page, _frame, sheet_name: str, _selectors: dict):
        self.created = _Sheet(sheet_name)
        self.frame.sheets.append(self.created)
        return self.created


def test_missing_configured_sheet_is_created_and_selected():
    sink = _TestSink(_Frame([_Sheet("Sheet1")]))
    sink._select_sheet_if_configured(
        _Page(), {"sheet_name": "73-84", "create_sheet_if_missing": True}
    )
    assert sink.created is not None
    assert sink.created.name == "73-84"
    assert sink.created.clicked is True


def test_sheet_creation_can_be_disabled_explicitly():
    sink = _TestSink(_Frame([_Sheet("Sheet1")]))
    try:
        sink._select_sheet_if_configured(
            _Page(), {"sheet_name": "73-84", "create_sheet_if_missing": False}
        )
    except RuntimeError as exc:
        assert "找不到工作表" in str(exc)
    else:
        raise AssertionError("missing sheet should fail when automatic creation is disabled")


def test_sheet_name_validation_rejects_wps_invalid_characters():
    try:
        QingTuiDocumentSink._validate_sheet_name("73/84")
    except RuntimeError as exc:
        assert "非法字符" in str(exc)
    else:
        raise AssertionError("invalid WPS sheet name should be rejected")


def test_binxin_projects_share_document_but_keep_distinct_sheets():
    root = Path(__file__).resolve().parents[1]
    config = json.loads(
        (root / "config" / "config.example.json").read_text(encoding="utf-8")
    )
    projects = {project["id"]: project for project in config["projects"]}
    expected = {
        "binxin_billet": "73-84",
        "binxin_billet_72": "1期-72",
        "binxin_billet_74": "1期-74",
        "binxin_76": "76",
        "binxin_77": "77",
    }
    document_url = "https://web.qingtui.com/drive/preview/EXAMPLE_DOCUMENT_ID"

    actual = {
        project_id: projects[project_id]["sink"]["selectors"]["sheet_name"]
        for project_id in expected
    }
    assert actual == expected
    assert len(set(actual.values())) == len(actual)
    assert all(
        projects[project_id]["sink"]["document_url"] == document_url
        for project_id in expected
    )


def test_written_table_comparison_accepts_wps_display_formatting():
    sink = QingTuiDocumentSink({})
    sink_cfg = {
        "format": {
            "columns": [
                {"field": "date", "header": "日期"},
                {"field": "sample_count", "header": "抽样数量"},
                {"field": "correct", "header": "正确数量"},
                {"field": "incorrect", "header": "错误数量"},
                {"field": "invalid", "header": "无效数量"},
                {"field": "accuracy", "header": "准确率"},
            ]
        }
    }
    specs = sink._column_specs(sink_cfg)
    expected = [
        ["日期", "抽样数量", "正确数量", "错误数量", "无效数量", "准确率"],
        ["2026-08-25", "50", "49", "1", "0", "98.0%"],
    ]
    actual = [
        ["日期", "抽样数量", "正确数量", "错误数量", "无效数量", "准确率"],
        ["2026/8/25", "50.0", "49", "1", "0", "98.00%"],
    ]

    assert sink._semantic_table_mismatch(
        expected, actual, specs, sink_cfg
    ) is None


def test_written_table_comparison_rejects_real_value_difference():
    sink = QingTuiDocumentSink({})
    sink_cfg = {
        "format": {
            "columns": [
                {"field": "date", "header": "日期"},
                {"field": "correct", "header": "正确数量"},
                {"field": "accuracy", "header": "准确率"},
            ]
        }
    }
    specs = sink._column_specs(sink_cfg)
    mismatch = sink._semantic_table_mismatch(
        [["日期", "正确数量", "准确率"], ["2026-08-25", "49", "98.0%"]],
        [["日期", "正确数量", "准确率"], ["2026/8/25", "48", "98.00%"]],
        specs,
        sink_cfg,
    )

    assert mismatch is not None
    assert "correct" in mismatch
