import json
from pathlib import Path
from urllib.parse import urlparse

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


def test_ruifeng_source_removes_duplicate_camera_groups_without_reordering():
    source = _source(["分组 A", "分组 A", "分组 B"])
    assert source.camera_groups == ("分组 A", "分组 B")


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
    assert source._column_indexes(headers) == {
        "timestamp": 2,
        "recognition": 4,
        "camera_group": 1,
        "image": 3,
    }


def test_login_selectors_match_saved_ruifeng_element_plus_page():
    source = _source()

    assert source._auth_selector(
        "username", source.DEFAULT_LOGIN_USERNAME_SELECTOR
    ) == 'form.el-form input.el-input__inner[placeholder="请输入用户名"]'
    assert source._auth_selector(
        "password", source.DEFAULT_LOGIN_PASSWORD_SELECTOR
    ) == 'form.el-form input.el-input__inner[placeholder="请输入密码"]'
    assert source._auth_selector(
        "login_button", source.DEFAULT_LOGIN_BUTTON_SELECTOR
    ) == "form.el-form button.login-button"


def test_route_precheck_reports_local_pysocks_dependency(monkeypatch):
    source = _source()
    monkeypatch.setattr("app.projects.ruifeng.source._pysocks", None)

    with pytest.raises(RuntimeError, match="审核平台本机缺少 Python 依赖 PySocks") as exc:
        source._ensure_route()
    assert "瑞丰目标机不需要 Python" in str(exc.value)


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
    remote = next(
        item
        for item in config["remote_connections"]
        if item["id"] == "atrust_remote_246"
    )
    deployment = remote["socks_over_rdp"]
    assert deployment["source_dir"] == "./dependencies"
    assert deployment["install_dir"] == (
        r"C:\Program Files\Cisdi_Data_Auto_Analyzer\dependencies"
    )
    assert (root / "dependencies" / deployment["plugin_dll"]).is_file()
    assert (root / "dependencies" / deployment["server_executable"]).is_file()
    for project in (trolley, ladle):
        proxy = project["source"]["proxy"]
        endpoint = urlparse(proxy["server"])
        assert (endpoint.hostname, endpoint.port) == (
            remote["socks_proxy"]["host"],
            remote["socks_proxy"]["port"],
        )
        assert "requests_url" not in proxy
        assert "healthcheck_url" not in proxy
        assert project["source"]["type"] == "ruifeng_history"
    assert trolley["diagnostics"]["enabled"] is False
    assert ladle["diagnostics"]["enabled"] is False
    assert trolley["sink"]["enabled"] is False
    assert ladle["sink"]["enabled"] is False
