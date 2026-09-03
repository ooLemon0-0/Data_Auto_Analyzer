import pytest

from app.projects.binxin_76.source import Binxin76HistorySource
from app.projects.binxin_77.source import Binxin77HistorySource


class FakeImage:
    def __init__(self, src: str | None):
        self.src = src

    def count(self) -> int:
        return int(self.src is not None)

    def get_attribute(self, name: str):
        return self.src if name == "src" else None


class FakeCell:
    def __init__(self, text: str = "", image_src: str | None = None):
        self.text = text
        self.image = FakeImage(image_src)

    def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str):
        assert selector == "img"
        return type("Images", (), {"first": self.image})()


class FakeCells:
    def __init__(self, cells):
        self.cells = cells

    def count(self) -> int:
        return len(self.cells)

    def nth(self, index: int):
        return self.cells[index]


class FakeRow:
    def __init__(self, cells):
        self.cells = FakeCells(cells)

    def locator(self, selector: str):
        assert selector == "td"
        return self.cells


class FakePage:
    url = "http://172.30.37.76:3000/#/history"


COLUMNS = {
    "sequence": 1,
    "image": 2,
    "timestamp": 3,
    "recognition_type": 4,
    "recognition": 5,
    "recognition_method": 6,
    "recognition_status": 7,
    "review_type": 8,
    "manual_review_value": 9,
}


@pytest.mark.parametrize(
    ("source_class", "station"),
    [(Binxin76HistorySource, "76"), (Binxin77HistorySource, "77")],
)
def test_edge_history_row_matches_supplied_76_dom(source_class, station):
    source = source_class(
        {"source": {"type": f"binxin_{station}_history", "station": station,
                    "selectors": {"columns": COLUMNS}}}
    )
    row = FakeRow(
        [
            FakeCell(),
            FakeCell("1"),
            FakeCell(image_src="/images/origin_1788435750227.jpg"),
            FakeCell("2026/09/03 19:44:19"),
            FakeCell("正常"),
            FakeCell("E6110477"),
            FakeCell("精准识别"),
            FakeCell("识别正常"),
            FakeCell("--"),
            FakeCell("--"),
            FakeCell("视频回放 人工复核"),
        ]
    )

    item = source._row_to_item(FakePage(), row, page_no=1, row_no=1)

    assert item is not None
    assert item.recognition_text == "E6110477"
    assert item.image_url == "http://172.30.37.76:3000/images/origin_1788435750227.jpg"
    assert item.metadata["timestamp"] == "2026/09/03 19:44:19"
    assert item.metadata["station"] == station
    assert item.metadata["recognition_type"] == "正常"
    assert item.metadata["recognition_method"] == "精准识别"
    assert item.metadata["recognition_status"] == "识别正常"
