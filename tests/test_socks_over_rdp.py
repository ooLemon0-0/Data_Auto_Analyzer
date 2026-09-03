from pathlib import Path

import pytest

from app.remote_access.socks_over_rdp import (
    SocksOverRDPInstaller,
    SocksOverRDPSetupError,
)


def _write_source_files(source_dir: Path) -> None:
    source_dir.mkdir(parents=True)
    (source_dir / "SocksOverRDP-Plugin.dll").write_bytes(b"plugin-v1")
    (source_dir / "SocksOverRDP-Server.exe").write_bytes(b"server-v1")
    (source_dir / "SocksOverRDP-RemoteBootstrap.ps1").write_text(
        "# bootstrap\n", encoding="utf-8"
    )
    (source_dir / "Install-SocksOverRDP-Remote.ps1").write_text(
        "# installer\n", encoding="utf-8"
    )


def _config(source_dir: Path, install_dir: Path) -> dict:
    return {
        "source_dir": str(source_dir),
        "install_dir": str(install_dir),
        "plugin_dll": "SocksOverRDP-Plugin.dll",
        "server_executable": "SocksOverRDP-Server.exe",
    }


def test_missing_installation_is_copied_registered_and_verified(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "source"
    install_dir = tmp_path / "installed"
    _write_source_files(source_dir)
    installer = SocksOverRDPInstaller()
    state = {"com": False, "addin": False, "settings": None}

    monkeypatch.setattr(
        installer,
        "_registration_state",
        lambda plugin, clsid, host, port: (state["com"], state["addin"]),
    )
    monkeypatch.setattr(
        installer,
        "_register_plugin",
        lambda plugin: state.update(com=True),
    )
    monkeypatch.setattr(
        installer,
        "_write_addin_settings",
        lambda clsid, host, port: state.update(
            addin=True,
            settings=(clsid, host, port),
        ),
    )

    result = installer.ensure(
        _config(source_dir, install_dir),
        {"host": "127.0.0.1", "port": 1080},
    )

    assert result["changed"] is True
    assert (install_dir / "SocksOverRDP-Plugin.dll").read_bytes() == b"plugin-v1"
    assert (install_dir / "SocksOverRDP-Server.exe").read_bytes() == b"server-v1"
    assert (install_dir / "SocksOverRDP-RemoteBootstrap.ps1").is_file()
    assert (install_dir / "Install-SocksOverRDP-Remote.ps1").is_file()
    assert (install_dir / "SocksOverRDP-RemoteSettings.json").is_file()
    assert state["settings"] == (
        SocksOverRDPInstaller.DEFAULT_CLSID,
        "127.0.0.1",
        1080,
    )


def test_valid_existing_installation_is_left_untouched(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    install_dir = tmp_path / "installed"
    _write_source_files(source_dir)
    installer = SocksOverRDPInstaller()

    monkeypatch.setattr(
        installer,
        "_registration_state",
        lambda plugin, clsid, host, port: (True, True),
    )
    monkeypatch.setattr(installer, "_register_plugin", lambda plugin: None)
    first_result = installer.ensure(
        _config(source_dir, install_dir),
        {"host": "127.0.0.1", "port": 1080},
    )
    monkeypatch.setattr(
        installer,
        "_register_plugin",
        lambda plugin: pytest.fail("valid registration must not be repeated"),
    )
    result = installer.ensure(
        _config(source_dir, install_dir),
        {"host": "127.0.0.1", "port": 1080},
    )

    assert first_result["changed"] is True
    assert result["changed"] is False


def test_source_dependencies_must_both_exist(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "SocksOverRDP-Plugin.dll").write_bytes(b"plugin")
    installer = SocksOverRDPInstaller()

    with pytest.raises(SocksOverRDPSetupError, match="源依赖缺失"):
        installer.ensure(
            _config(source_dir, tmp_path / "installed"),
            {"host": "127.0.0.1", "port": 1080},
        )


def test_remote_scripts_install_reconnect_task_and_watchdog():
    root = Path(__file__).resolve().parents[1] / "dependencies"
    installer = (root / "Install-SocksOverRDP-Remote.ps1").read_text(
        encoding="utf-8"
    )
    bootstrap = (root / "SocksOverRDP-RemoteBootstrap.ps1").read_text(
        encoding="utf-8"
    )

    assert "<MultipleInstancesPolicy>StopExisting</MultipleInstancesPolicy>" in installer
    assert '$watchdogTaskName = "$taskName Watchdog"' in installer
    assert "<Interval>PT1M</Interval>" in installer
    assert "PT30S" not in installer
    assert "-EnsureOnly" in installer
    assert "[switch]$EnsureOnly" in bootstrap
    assert "if ($EnsureOnly -and $sessionProcesses.Count -gt 0)" in bootstrap
    assert "if (-not (Test-Path -LiteralPath $targetServer -PathType Leaf))" in bootstrap


def test_remote_settings_change_does_not_reregister_valid_plugin(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "source"
    install_dir = tmp_path / "installed"
    _write_source_files(source_dir)
    installer = SocksOverRDPInstaller()
    registrations = []
    addin_writes = []

    monkeypatch.setattr(
        installer,
        "_registration_state",
        lambda plugin, clsid, host, port: (True, True),
    )
    monkeypatch.setattr(
        installer,
        "_register_plugin",
        lambda plugin: registrations.append(plugin),
    )
    monkeypatch.setattr(
        installer,
        "_write_addin_settings",
        lambda *args: addin_writes.append(args),
    )

    installer.ensure(
        _config(source_dir, install_dir),
        {"host": "127.0.0.1", "port": 1080},
    )
    registrations.clear()
    addin_writes.clear()
    changed_config = _config(source_dir, install_dir)
    changed_config["remote_wait_seconds"] = 120
    result = installer.ensure(
        changed_config,
        {"host": "127.0.0.1", "port": 1080},
    )

    assert result["changed"] is True
    assert registrations == []
    assert addin_writes == []
