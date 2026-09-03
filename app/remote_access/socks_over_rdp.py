from __future__ import annotations

import filecmp
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only integration
    winreg = None


class SocksOverRDPSetupError(RuntimeError):
    """SocksOverRDP cannot be verified or provisioned safely."""


class SocksOverRDPInstaller:
    DEFAULT_CLSID = "{61715a27-63a8-4dc1-9857-83eb00fb60a3}"
    REMOTE_BOOTSTRAP_SCRIPT = "SocksOverRDP-RemoteBootstrap.ps1"
    REMOTE_INSTALLER_SCRIPT = "Install-SocksOverRDP-Remote.ps1"
    REMOTE_SETTINGS_FILE = "SocksOverRDP-RemoteSettings.json"
    DEFAULT_REMOTE_TASK_NAME = "SocksOverRDP Server"
    ADDIN_KEY = (
        r"Software\Microsoft\Terminal Server Client\Default\AddIns"
        r"\SocksOverRDP-Plugin"
    )
    PROJECT_ROOT = Path(__file__).resolve().parents[2]

    @classmethod
    def _resolve_source_dir(cls, value: Any) -> Path:
        path = Path(str(value or "./dependencies")).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (cls.PROJECT_ROOT / path).resolve()

    @staticmethod
    def _required_filename(config: dict[str, Any], key: str, default: str) -> str:
        filename = str(config.get(key, default)).strip()
        if not filename or Path(filename).name != filename:
            raise SocksOverRDPSetupError(f"socks_over_rdp.{key} 必须是文件名")
        return filename

    @staticmethod
    def _normalised_path(value: Any) -> str:
        text = os.path.expandvars(str(value or "").strip().strip('"'))
        if not text:
            return ""
        return os.path.normcase(os.path.abspath(text))

    @staticmethod
    def _registry_value(root, subkey: str, name: str | None):
        if winreg is None:
            return None
        access_modes = [winreg.KEY_READ]
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            access_modes = [
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
                winreg.KEY_READ,
            ]
        for access in access_modes:
            try:
                with winreg.OpenKey(root, subkey, 0, access) as key:
                    return winreg.QueryValueEx(key, name)[0]
            except OSError:
                continue
        return None

    def _registration_state(
        self,
        plugin_path: Path,
        clsid: str,
        host: str,
        port: int,
    ) -> tuple[bool, bool]:
        if winreg is None:
            return False, False
        registered_path = self._registry_value(
            winreg.HKEY_CLASSES_ROOT,
            rf"CLSID\{clsid}\InprocServer32",
            None,
        )
        addin_name = self._registry_value(
            winreg.HKEY_CURRENT_USER,
            self.ADDIN_KEY,
            "Name",
        )
        enabled = self._registry_value(
            winreg.HKEY_CURRENT_USER,
            self.ADDIN_KEY,
            "enabled",
        )
        registered_host = self._registry_value(
            winreg.HKEY_CURRENT_USER,
            self.ADDIN_KEY,
            "ip",
        )
        registered_port = self._registry_value(
            winreg.HKEY_CURRENT_USER,
            self.ADDIN_KEY,
            "port",
        )
        try:
            port_matches = int(registered_port) == port
        except (TypeError, ValueError):
            port_matches = False
        try:
            enabled_matches = int(enabled) == 1
        except (TypeError, ValueError):
            enabled_matches = False
        com_registered = (
            self._normalised_path(registered_path)
            == self._normalised_path(plugin_path)
        )
        addin_configured = (
            str(addin_name or "").lower() == clsid.lower()
            and enabled_matches
            and str(registered_host or "").strip() == host
            and port_matches
        )
        return com_registered, addin_configured

    def _is_registered(
        self,
        plugin_path: Path,
        clsid: str,
        host: str,
        port: int,
    ) -> bool:
        return all(self._registration_state(plugin_path, clsid, host, port))

    @staticmethod
    def _copy_if_needed(source: Path, destination: Path) -> None:
        if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            return
        shutil.copy2(source, destination)

    @staticmethod
    def _files_match(source: Path, destination: Path) -> bool:
        return (
            destination.is_file()
            and filecmp.cmp(source, destination, shallow=False)
        )

    @staticmethod
    def _redirected_client_path(local_path: Path) -> str:
        windows_path = PureWindowsPath(str(local_path))
        drive = windows_path.drive
        if len(drive) != 2 or drive[1] != ":":
            raise SocksOverRDPSetupError(
                "socks_over_rdp.install_dir 必须位于可重定向的本机盘符"
            )
        tail = PureWindowsPath(*windows_path.parts[1:])
        return str(PureWindowsPath(rf"\\tsclient\{drive[0]}") / tail)

    @staticmethod
    def _write_text_if_changed(path: Path, content: str) -> bool:
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == content:
                return False
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise SocksOverRDPSetupError(
                f"无法写入 SocksOverRDP 远端部署配置：{path}"
            ) from exc
        return True

    def remote_installer_command(self, config: dict[str, Any]) -> str:
        install_dir_text = str(config.get("install_dir", "")).strip()
        if not install_dir_text:
            raise SocksOverRDPSetupError("socks_over_rdp.install_dir 未配置")
        install_dir = Path(install_dir_text).expanduser()
        if not install_dir.is_absolute():
            raise SocksOverRDPSetupError(
                "socks_over_rdp.install_dir 必须是绝对路径"
            )
        redirected_installer = str(
            PureWindowsPath(self._redirected_client_path(install_dir.resolve()))
            / self.REMOTE_INSTALLER_SCRIPT
        )
        return (
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -File '
            f'"{redirected_installer}"'
        )

    @staticmethod
    def _register_plugin(plugin_path: Path) -> None:
        windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        regsvr32 = windows_dir / "System32" / "regsvr32.exe"
        if not regsvr32.is_file():
            raise SocksOverRDPSetupError(f"未找到 regsvr32.exe：{regsvr32}")
        try:
            completed = subprocess.run(
                [str(regsvr32), "/s", str(plugin_path)],
                cwd=str(plugin_path.parent),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except OSError as exc:
            raise SocksOverRDPSetupError("无法启动 regsvr32 注册 SocksOverRDP") from exc
        if completed.returncode != 0:
            raise SocksOverRDPSetupError(
                "SocksOverRDP DLL 注册失败，"
                f"regsvr32 返回 {completed.returncode}。请以管理员身份运行本程序。"
            )

    @staticmethod
    def _write_addin_settings(clsid: str, host: str, port: int) -> None:
        if winreg is None:
            raise SocksOverRDPSetupError("SocksOverRDP 仅支持 Windows")
        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                SocksOverRDPInstaller.ADDIN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "Name", 0, winreg.REG_SZ, clsid)
                winreg.SetValueEx(key, "enabled", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ip", 0, winreg.REG_SZ, host)
                winreg.SetValueEx(key, "port", 0, winreg.REG_SZ, str(port))
        except OSError as exc:
            raise SocksOverRDPSetupError(
                "无法写入当前用户的 SocksOverRDP 插件配置"
            ) from exc

    def ensure(
        self,
        config: dict[str, Any],
        proxy: dict[str, Any],
    ) -> dict[str, Any]:
        if os.name != "nt" or winreg is None:
            raise SocksOverRDPSetupError("SocksOverRDP 自检仅支持 Windows")

        source_dir = self._resolve_source_dir(config.get("source_dir"))
        install_dir_text = str(config.get("install_dir", "")).strip()
        if not install_dir_text:
            raise SocksOverRDPSetupError("socks_over_rdp.install_dir 未配置")
        install_dir = Path(install_dir_text).expanduser()
        if not install_dir.is_absolute():
            raise SocksOverRDPSetupError("socks_over_rdp.install_dir 必须是绝对路径")
        install_dir = install_dir.resolve()

        plugin_name = self._required_filename(
            config, "plugin_dll", "SocksOverRDP-Plugin.dll"
        )
        server_name = self._required_filename(
            config, "server_executable", "SocksOverRDP-Server.exe"
        )
        clsid = self.DEFAULT_CLSID

        source_plugin = source_dir / plugin_name
        source_server = source_dir / server_name
        source_bootstrap = source_dir / self.REMOTE_BOOTSTRAP_SCRIPT
        source_installer = source_dir / self.REMOTE_INSTALLER_SCRIPT
        missing = [
            str(path)
            for path in (
                source_plugin,
                source_server,
                source_bootstrap,
                source_installer,
            )
            if not path.is_file()
        ]
        if missing:
            raise SocksOverRDPSetupError(
                "SocksOverRDP 源依赖缺失：" + "、".join(missing)
            )

        target_plugin = install_dir / plugin_name
        target_server = install_dir / server_name
        target_bootstrap = install_dir / self.REMOTE_BOOTSTRAP_SCRIPT
        target_installer = install_dir / self.REMOTE_INSTALLER_SCRIPT
        target_settings = install_dir / self.REMOTE_SETTINGS_FILE
        host = str(proxy.get("host", "127.0.0.1")).strip()
        port = int(proxy.get("port", 1080))

        remote_install_text = str(
            config.get("remote_install_dir") or install_dir
        ).strip()
        remote_install_dir = PureWindowsPath(remote_install_text)
        if not remote_install_dir.is_absolute():
            raise SocksOverRDPSetupError(
                "socks_over_rdp.remote_install_dir 必须是绝对路径"
            )
        remote_task_name = str(
            config.get("remote_task_name") or self.DEFAULT_REMOTE_TASK_NAME
        ).strip()
        if not remote_task_name:
            raise SocksOverRDPSetupError("socks_over_rdp.remote_task_name 不能为空")
        try:
            remote_wait_seconds = int(config.get("remote_wait_seconds", 90))
        except (TypeError, ValueError) as exc:
            raise SocksOverRDPSetupError(
                "socks_over_rdp.remote_wait_seconds 格式无效"
            ) from exc
        if not 1 <= remote_wait_seconds <= 300:
            raise SocksOverRDPSetupError(
                "socks_over_rdp.remote_wait_seconds 必须在 1 到 300 之间"
            )

        settings_payload = {
            "bootstrap_script": self.REMOTE_BOOTSTRAP_SCRIPT,
            "client_source_directory": self._redirected_client_path(install_dir),
            "remote_install_directory": str(remote_install_dir),
            "server_executable": server_name,
            "task_name": remote_task_name,
            "wait_seconds": remote_wait_seconds,
        }
        settings_content = json.dumps(
            settings_payload,
            ensure_ascii=False,
            indent=2,
        ) + "\n"

        payload_pairs = (
            (source_plugin, target_plugin),
            (source_server, target_server),
            (source_bootstrap, target_bootstrap),
            (source_installer, target_installer),
        )
        payload_matches = all(
            self._files_match(source, target)
            for source, target in payload_pairs
        )
        settings_match = (
            target_settings.is_file()
            and target_settings.read_text(encoding="utf-8") == settings_content
        )
        com_registered, addin_configured = self._registration_state(
            target_plugin,
            clsid,
            host,
            port,
        )
        if payload_matches and settings_match and com_registered and addin_configured:
            return {
                "changed": False,
                "plugin_path": str(target_plugin),
                "server_path": str(target_server),
                "remote_installer_path": str(target_installer),
            }

        plugin_changed = not self._files_match(source_plugin, target_plugin)
        try:
            install_dir.mkdir(parents=True, exist_ok=True)
            for source, target in payload_pairs:
                self._copy_if_needed(source, target)
        except OSError as exc:
            raise SocksOverRDPSetupError(
                f"无法部署 SocksOverRDP 到 {install_dir}。请以管理员身份运行本程序。"
            ) from exc

        self._write_text_if_changed(target_settings, settings_content)
        if plugin_changed or not com_registered:
            self._register_plugin(target_plugin)
        if not addin_configured:
            self._write_addin_settings(clsid, host, port)
        if not self._is_registered(target_plugin, clsid, host, port):
            raise SocksOverRDPSetupError(
                "SocksOverRDP 注册命令已执行，但 COM 或 RDP AddIns 注册校验失败"
            )
        return {
            "changed": True,
            "plugin_path": str(target_plugin),
            "server_path": str(target_server),
            "remote_installer_path": str(target_installer),
        }


socks_over_rdp_installer = SocksOverRDPInstaller()
