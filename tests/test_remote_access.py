import pytest

from app.remote_access.service import RemoteAccessError, RemoteAccessService


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
        "vpn": {
            "type": "atrust",
            "access_url": "https://60.2.40.222:4436",
            "username": "tangzhou",
            "password": "",
        },
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
        "vpn": {
            "type": "atrust",
            "access_url": "https://60.2.40.222:4436",
            "username": "tangzhou",
        },
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
        "vpn": {"type": "atrust"},
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
    monkeypatch.setattr(service, "_launch_rdp", lambda rdp: events.append("launch_rdp"))

    service._run("test", config)

    assert events == [
        "launch_atrust",
        ("wait", "10.100.205.246", 3389, 180, "aTrust 连通"),
        "launch_rdp",
        ("wait", "127.0.0.1", 1080, 90, "SocksOverRDP 代理"),
    ]
    assert service._jobs["test"]["phase"] == "ready"


def test_remote_connection_rejects_invalid_socks_proxy_port():
    service = RemoteAccessService()
    config = {
        "enabled": True,
        "vpn": {
            "type": "atrust",
            "access_url": "https://60.2.40.222:4436",
            "username": "tangzhou",
        },
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
    }
    with pytest.raises(RemoteAccessError, match="socks_proxy"):
        service._validate(config)

