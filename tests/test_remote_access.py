import pytest

from app.remote_access.service import RemoteAccessError, RemoteAccessService


def test_remote_connection_status_does_not_expose_credentials():
    status = RemoteAccessService().status("atrust_remote_246")
    assert status["rdp_target"] == "10.100.205.246:3389"
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

