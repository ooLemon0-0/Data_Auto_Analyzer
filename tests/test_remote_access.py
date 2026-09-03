from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.remote_access.service import RemoteAccessError, RemoteAccessService
from app.remote_access.socks_over_rdp import SocksOverRDPSetupError


def _vpn(**values):
    return {
        "type": "atrust",
        "access_address": "https://vpn.example.com",
        **values,
    }


def _socks_over_rdp():
    return {
        "source_dir": "./dependencies",
        "install_dir": r"C:\Program Files\Cisdi_Data_Auto_Analyzer\dependencies",
        "remote_install_dir": r"C:\Program Files\Cisdi_Data_Auto_Analyzer\dependencies",
        "remote_wait_seconds": 90,
        "plugin_dll": "SocksOverRDP-Plugin.dll",
        "server_executable": "SocksOverRDP-Server.exe",
    }


def test_remote_connection_status_does_not_expose_credentials():
    status = RemoteAccessService().status("atrust_remote_246")
    assert status["rdp_target"] == "10.100.205.246:3389"
    assert status["proxy_target"] == "127.0.0.1:1080"
    assert "password" not in status
    assert "username" not in status


def test_remote_connection_refuses_launch_until_rdp_password_is_configured():
    service = RemoteAccessService()
    config = {
        "id": "test",
        "enabled": True,
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.246",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "",
        },
    }
    with pytest.raises(RemoteAccessError, match="RDP password 未配置"):
        service._validate(config)


def test_remote_connection_rejects_invalid_rdp_network_config():
    service = RemoteAccessService()
    config = {
        "id": "test",
        "enabled": True,
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.999",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "secret",
        },
    }
    with pytest.raises(RemoteAccessError, match="格式无效"):
        service._validate(config)


def test_remote_connection_waits_for_socks_proxy_after_launching_rdp(monkeypatch):
    service = RemoteAccessService()
    events = []
    config = {
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.246",
            "port": 3389,
            "connect_timeout_seconds": 180,
        },
        "socks_proxy": {
            "host": "127.0.0.1",
            "port": 1080,
            "connect_timeout_seconds": 90,
        },
        "socks_over_rdp": _socks_over_rdp(),
    }

    monkeypatch.setattr(
        service, "_launch_atrust", lambda vpn: events.append("launch_atrust")
    )
    monkeypatch.setattr(
        service,
        "_wait_for_port",
        lambda host, port, timeout, purpose="": events.append(
            ("wait", host, port, timeout, purpose)
        ),
    )
    monkeypatch.setattr(
        service,
        "_launch_rdp",
        lambda rdp, deployment: events.append(("launch_rdp", deployment)),
    )

    service._run("test", config)

    assert events == [
        "launch_atrust",
        ("wait", "10.100.205.246", 3389, 180, "aTrust 连通"),
        ("launch_rdp", config["socks_over_rdp"]),
        ("wait", "127.0.0.1", 1080, 90, "SocksOverRDP 代理"),
    ]
    assert service._jobs["test"]["phase"] == "ready"


def test_remote_connection_rejects_invalid_socks_proxy_port():
    service = RemoteAccessService()
    config = {
        "enabled": True,
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.246",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "secret",
        },
        "socks_proxy": {
            "host": "127.0.0.1",
            "port": 70000,
        },
        "socks_over_rdp": _socks_over_rdp(),
    }
    with pytest.raises(RemoteAccessError, match="socks_proxy"):
        service._validate(config)


def test_remote_connection_accepts_desktop_client_session_config():
    service = RemoteAccessService()
    config = {
        "enabled": True,
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.246",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "secret",
        },
        "socks_proxy": {"host": "127.0.0.1", "port": 1080},
        "socks_over_rdp": _socks_over_rdp(),
    }

    service._validate(config)


def test_remote_connection_requires_socks_deployment_config():
    service = RemoteAccessService()
    config = {
        "enabled": True,
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.246",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "secret",
        },
        "socks_proxy": {"host": "127.0.0.1", "port": 1080},
    }

    with pytest.raises(RemoteAccessError, match="socks_over_rdp"):
        service._validate(config)


def test_socks_setup_failure_is_exposed_as_remote_access_error(monkeypatch):
    service = RemoteAccessService()
    config = {
        "socks_over_rdp": _socks_over_rdp(),
        "socks_proxy": {"host": "127.0.0.1", "port": 1080},
    }

    def fail(*args):
        raise SocksOverRDPSetupError("registration failed")

    monkeypatch.setattr(
        "app.remote_access.service.socks_over_rdp_installer.ensure",
        fail,
    )

    with pytest.raises(RemoteAccessError, match="registration failed"):
        service._ensure_socks_over_rdp(config)


def test_atrust_launch_starts_desktop_client_without_opening_portal(monkeypatch):
    service = RemoteAccessService()
    executable = Path(service.ATRUST_CANDIDATES[0])
    events = []

    monkeypatch.setattr(service, "_atrust_executable", lambda vpn: executable)
    monkeypatch.setattr(
        service,
        "_prepare_atrust_address",
        lambda vpn, path: events.append(("address", vpn, path)),
    )
    monkeypatch.setattr(
        "app.remote_access.service.subprocess.Popen",
        lambda command, cwd, close_fds: events.append((command, cwd, close_fds)),
    )

    vpn = _vpn()
    service._launch_atrust(vpn)

    assert events == [
        ("address", vpn, executable),
        (
            [str(executable), "--", "-s", "autostart"],
            str(executable.parent),
            True,
        ),
    ]


def test_atrust_address_is_provisioned_from_json(tmp_path):
    service = RemoteAccessService()
    address_file = tmp_path / "addr.conf"
    executable = tmp_path / "aTrustTray" / "aTrustTray.exe"

    service._prepare_atrust_address(
        _vpn(
            access_address="https://VPN.EXAMPLE.COM:443/",
            address_file=str(address_file),
        ),
        executable,
    )

    assert address_file.read_text(encoding="utf-8") == "https://vpn.example.com\n"


def test_atrust_plaintext_login_fields_are_rejected():
    service = RemoteAccessService()
    config = {
        "enabled": True,
        "vpn": _vpn(username="operator", password="secret"),
        "rdp": {
            "host": "10.100.205.246",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "secret",
        },
    }

    with pytest.raises(RemoteAccessError, match="不会读取这些 JSON 字段"):
        service._validate(config)


def test_generated_rdp_file_forces_required_client_drive_redirection(
    tmp_path, monkeypatch
):
    service = RemoteAccessService()
    monkeypatch.chdir(tmp_path)

    rdp_path = service._write_rdp_file(
        {
            "host": "10.100.205.246",
            "port": 3389,
            "username": r"DESKTOP-G8OVQHR\Administrator",
        },
        _socks_over_rdp(),
    )

    content = rdp_path.read_text(encoding="utf-16")
    assert "full address:s:10.100.205.246:3389" in content
    assert r"username:s:DESKTOP-G8OVQHR\Administrator" in content
    assert r"drivestoredirect:s:C:\;" in content
    assert "prompt for credentials:i:0" in content
    assert "authentication level:i:2" in content


def test_remote_bootstrap_hint_contains_copyable_config_derived_command(
    monkeypatch,
):
    service = RemoteAccessService()
    config = {
        "id": "test",
        "name": "瑞丰远程连接",
        "rdp": {"host": "10.100.205.246", "port": 3389},
        "socks_proxy": {"host": "127.0.0.1", "port": 1080},
        "socks_over_rdp": _socks_over_rdp(),
    }

    class FakeSettings:
        @staticmethod
        def remote_connection(connection_id):
            assert connection_id == "test"
            return config

    service._jobs["test"] = {
        "phase": "waiting_proxy",
        "message": "正在等待代理",
        "proxy_wait_started_at": (
            datetime.now() - timedelta(seconds=30)
        ).isoformat(timespec="seconds"),
    }
    monkeypatch.setattr("app.remote_access.service.settings", FakeSettings())
    monkeypatch.setattr(
        service,
        "_port_reachable",
        lambda host, port, timeout=1: port == 3389,
    )

    status = service.status("test")

    assert status["rdp_reachable"] is True
    assert status["proxy_reachable"] is False
    assert status["operator_action"]["kind"] == (
        "socks_over_rdp_remote_bootstrap"
    )
    assert status["operator_action"]["command"] == (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
        '"\\\\tsclient\\C\\Program Files\\Cisdi_Data_Auto_Analyzer'
        '\\dependencies\\Install-SocksOverRDP-Remote.ps1"'
    )
    assert "password" not in status["operator_action"]["command"].lower()


def test_remote_bootstrap_hint_is_not_shown_when_rdp_is_unreachable():
    service = RemoteAccessService()
    action = service._remote_setup_action(
        {"socks_over_rdp": _socks_over_rdp()},
        {
            "phase": "waiting_proxy",
            "proxy_wait_started_at": (
                datetime.now() - timedelta(seconds=30)
            ).isoformat(timespec="seconds"),
        },
        rdp_reachable=False,
        proxy_reachable=False,
    )

    assert action is None


def test_ready_target_port_is_reused_without_relaunching_remote_chain(monkeypatch):
    service = RemoteAccessService()
    config = {
        "id": "test",
        "enabled": True,
        "vpn": _vpn(),
        "rdp": {
            "host": "10.100.205.246",
            "subnet_mask": "255.255.255.0",
            "username": "administrator",
            "password": "secret",
        },
        "socks_proxy": {"host": "127.0.0.1", "port": 1080},
        "socks_over_rdp": _socks_over_rdp(),
    }

    class FakeSettings:
        @staticmethod
        def remote_connection(connection_id):
            assert connection_id == "test"
            return config

    launches = []
    preflights = []
    monkeypatch.setattr("app.remote_access.service.settings", FakeSettings())
    monkeypatch.setattr(
        service,
        "_ensure_socks_over_rdp",
        lambda connection: preflights.append(connection),
    )
    monkeypatch.setattr(service, "_port_reachable", lambda host, port, timeout=1: True)
    monkeypatch.setattr(service, "_launch_atrust", lambda vpn: launches.append(vpn))

    status = service.launch("test")

    assert status["phase"] == "ready"
    assert status["proxy_reachable"] is True
    assert preflights == [config]
    assert launches == []
