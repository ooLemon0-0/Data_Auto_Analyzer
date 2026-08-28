import json
from pathlib import Path

import pytest

from app.projects.ruifeng.source import RuifengHistorySource


def _source(groups=None) -> RuifengHistorySource:
    return RuifengHistorySource(
        {
            "source": {
                "history_url": "http://192.100.100.150:3000/#/history",
                "camera_groups": ["渣跨1号废钢台车"] if groups is None else groups,
                "proxy": {"server": "socks5://127.0.0.1:1080"},
            }
        }
    )


def test_ruifeng_source_requires_explicit_camera_groups():
    with pytest.raises(ValueError, match="camera_groups"):
        _source([])


def test_socks_proxy_uses_remote_hostname_resolution_for_requests():
    source = _source()
    assert source._browser_proxy_server() == "socks5://127.0.0.1:1080"
    assert source._requests_proxies() == {
        "http": "socks5h://127.0.0.1:1080",
        "https": "socks5h://127.0.0.1:1080",
    }


def test_table_columns_are_resolved_by_heading_instead_of_binxin_indexes():
    source = _source()
    headers = ["序号", "相机分组", "采集时间", "原始图片", "OCR识别结果"]
    assert source._column_index(headers, "camera_group") == 1
    assert source._column_index(headers, "timestamp") == 2
    assert source._column_index(headers, "image") == 3
    assert source._column_index(headers, "recognition") == 4


def test_ruifeng_projects_have_disjoint_expected_camera_groups():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config" / "config.example.json").read_text(encoding="utf-8"))
    projects = {project["id"]: project for project in config["projects"]}

    trolley = projects["ruifeng_trolley"]
    ladle = projects["ruifeng_ladle"]
    assert trolley["source"]["camera_groups"] == [
        "渣跨1号废钢台车",
        "渣跨2号废钢台车",
    ]
    assert ladle["source"]["camera_groups"] == [
        "转炉吊包位",
        "钢水跨精炼2号台车",
        "钢水跨精炼1号台车",
    ]
    assert set(trolley["source"]["camera_groups"]).isdisjoint(
        ladle["source"]["camera_groups"]
    )
    assert trolley["source"]["remote_connection_id"] == "atrust_remote_246"
    assert ladle["source"]["remote_connection_id"] == "atrust_remote_246"
    assert trolley["diagnostics"]["enabled"] is False
    assert ladle["diagnostics"]["enabled"] is False
    assert trolley["sink"]["enabled"] is False
    assert ladle["sink"]["enabled"] is False
