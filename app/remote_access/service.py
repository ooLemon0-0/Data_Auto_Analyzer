from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import ipaddress
import logging
import os
from pathlib import Path, PureWindowsPath
import socket
import subprocess
from threading import RLock, Thread
import time
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
from app.remote_access.socks_over_rdp import (
    SocksOverRDPSetupError,
    socks_over_rdp_installer,
)


logger = logging.getLogger("uvicorn.error")


class RemoteAccessError(RuntimeError):
    """Safe, user-readable remote access failure."""


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class RemoteAccessService:
    REMOTE_SETUP_HINT_SECONDS = 20
    ACTIVE_PHASES = {
        "starting_vpn",
        "waiting_vpn",
        "launching_rdp",
        "waiting_proxy",
    }
    ATRUST_CANDIDATES = [
        r"C:\Program Files (x86)\Sangfor\aTrust\aTrustTray\aTrustTray.exe",
        r"C:\Program Files\Sangfor\aTrust\aTrustTray\aTrustTray.exe",
    ]
    ATRUST_START_TYPES = {"autostart", "defaultstart"}

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _set_status(self, connection_id: str, **values: Any) -> None:
        with self._lock:
            current = self._jobs.setdefault(connection_id, {})
            previous_phase = current.get("phase")
            previous_message = current.get("message")
            current.update(values, updated_at=self._now())
            phase = current.get("phase")
            message = current.get("message")
        if phase != previous_phase or message != previous_message:
            logger.info(
                "远程连接状态: connection_id=%s phase=%s message=%s",
                connection_id,
                phase,
                message,
            )

    def status(self, connection_id: str) -> dict[str, Any]:
        config = settings.remote_connection(connection_id)
        rdp = config.get("rdp", {})
        host, port = str(rdp.get("host", "")), int(rdp.get("port", 3389))
        proxy = config.get("socks_proxy") or {}
        proxy_host = str(proxy.get("host", "")).strip()
        proxy_port = int(proxy.get("port", 1080))
        with self._lock:
            job = dict(self._jobs.get(connection_id, {}))
        rdp_reachable = self._port_reachable(host, port, timeout=0.4)
        proxy_reachable = (
            self._port_reachable(proxy_host, proxy_port, timeout=0.2)
            if proxy_host
            else None
        )
        return {
            "connection_id": connection_id,
            "name": config.get("name", connection_id),
            "phase": job.get("phase", "idle"),
            "message": job.get("message", "尚未启动"),
            "started_at": job.get("started_at"),
            "updated_at": job.get("updated_at"),
            "rdp_target": f"{host}:{port}",
            "rdp_reachable": rdp_reachable,
            "proxy_target": f"{proxy_host}:{proxy_port}" if proxy_host else None,
            "proxy_reachable": proxy_reachable,
            "operator_action": self._remote_setup_action(
                config,
                job,
                rdp_reachable,
                proxy_reachable,
            ),
        }

    def _remote_setup_action(
        self,
        config: dict[str, Any],
        job: dict[str, Any],
        rdp_reachable: bool,
        proxy_reachable: bool | None,
    ) -> dict[str, str] | None:
        deployment = config.get("socks_over_rdp") or {}
        if not deployment or not rdp_reachable or proxy_reachable is not False:
            return None

        phase = str(job.get("phase", ""))
        if phase == "waiting_proxy":
            started_text = str(
                job.get("proxy_wait_started_at") or job.get("updated_at") or ""
            )
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(started_text)).total_seconds()
            except ValueError:
                return None
            if elapsed < self.REMOTE_SETUP_HINT_SECONDS:
                return None
        elif not (
            phase == "failed" and job.get("failure_phase") == "waiting_proxy"
        ):
            return None

        try:
            command = socks_over_rdp_installer.remote_installer_command(deployment)
        except SocksOverRDPSetupError:
            return None
        return {
            "kind": "socks_over_rdp_remote_bootstrap",
            "title": "瑞丰远端常驻服务未启动",
            "message": (
                "已能访问远程电脑，但本机还没有建立访问瑞丰网站所需的"
                " SocksOverRDP 通道。下面的命令只用于首次安装或修复常驻任务，"
                "不需要在每次连接时运行；命令必须在瑞丰目标机中执行。"
            ),
            "command": command,
            "rdp_target": (
                f"{config.get('rdp', {}).get('host', '')}:"
                f"{config.get('rdp', {}).get('port', 3389)}"
            ),
            "proxy_target": (
                f"{config.get('socks_proxy', {}).get('host', '127.0.0.1')}:"
                f"{config.get('socks_proxy', {}).get('port', 1080)}"
            ),
        }

    def launch(self, connection_id: str) -> dict[str, Any]:
        config = settings.remote_connection(connection_id)
        self._validate(config)
        self._ensure_socks_over_rdp(config)
        proxy = config.get("socks_proxy") or {}
        proxy_host = str(proxy.get("host", "")).strip()
        proxy_port = int(proxy.get("port", 1080))
        if proxy_host and self._port_reachable(proxy_host, proxy_port, timeout=0.2):
            now = self._now()
            with self._lock:
                self._jobs[connection_id] = {
                    "phase": "ready",
                    "message": "SocksOverRDP 代理已就绪",
                    "started_at": now,
                    "updated_at": now,
                }
            logger.info(
                "远程连接复用现有 SOCKS 代理: connection_id=%s proxy=%s:%s",
                connection_id,
                proxy_host,
                proxy_port,
            )
            return self.status(connection_id)
        with self._lock:
            current = self._jobs.get(connection_id, {})
            if current.get("phase") in self.ACTIVE_PHASES:
                return self.status(connection_id)
            started_at = self._now()
            self._jobs[connection_id] = {
                "phase": "starting_vpn",
                "message": "正在启动 aTrust",
                "started_at": started_at,
                "updated_at": started_at,
            }
        logger.info("开始建立远程连接: connection_id=%s", connection_id)
        Thread(
            target=self._run,
            args=(connection_id, config),
            name=f"remote-access-{connection_id}",
            daemon=True,
        ).start()
        return self.status(connection_id)

    def _run(self, connection_id: str, config: dict[str, Any]) -> None:
        try:
            self._launch_atrust(config["vpn"])
            self._set_status(
                connection_id,
                phase="waiting_vpn",
                message=(
                    "aTrust 正在恢复已保存的登录会话；"
                    "首次使用时需在客户端中完成一次认证"
                ),
            )
            rdp = config["rdp"]
            host = str(rdp["host"])
            port = int(rdp.get("port", 3389))
            timeout = int(rdp.get("connect_timeout_seconds", 180))
            self._wait_for_port(host, port, timeout, "aTrust 连通")
            self._set_status(
                connection_id,
                phase="launching_rdp",
                message="VPN 已连通，正在启动 Windows 远程桌面",
            )
            self._launch_rdp(rdp, config.get("socks_over_rdp") or {})
            proxy = config.get("socks_proxy") or {}
            proxy_host = str(proxy.get("host", "")).strip()
            if proxy_host:
                proxy_port = int(proxy.get("port", 1080))
                proxy_timeout = int(proxy.get("connect_timeout_seconds", 180))
                self._set_status(
                    connection_id,
                    phase="waiting_proxy",
                    message="远程桌面已启动，正在等待 SocksOverRDP 代理",
                    proxy_wait_started_at=self._now(),
                )
                self._wait_for_port(
                    proxy_host,
                    proxy_port,
                    proxy_timeout,
                    "SocksOverRDP 代理",
                )
                self._set_status(
                    connection_id,
                    phase="ready",
                    message="aTrust、远程桌面和 SocksOverRDP 均已就绪",
                )
            else:
                self._set_status(
                    connection_id,
                    phase="rdp_started",
                    message="Windows 远程桌面已启动",
                )
        except Exception as exc:
            with self._lock:
                failure_phase = self._jobs.get(connection_id, {}).get("phase")
            logger.exception(
                "远程连接后台任务失败: connection_id=%s phase=%s error=%s",
                connection_id,
                failure_phase,
                exc,
            )
            self._set_status(
                connection_id,
                phase="failed",
                failure_phase=failure_phase,
                message=str(exc),
            )

    def _validate(self, config: dict[str, Any]) -> None:
        if os.name != "nt":
            raise RemoteAccessError("aTrust/RDP 链路仅支持 Windows")
        if not config.get("enabled", True):
            raise RemoteAccessError("该远程连接已禁用")
        vpn = config.get("vpn") or {}
        rdp = config.get("rdp") or {}
        if vpn.get("type") != "atrust":
            raise RemoteAccessError("remote connection 的 vpn.type 必须是 atrust")
        self._normalise_atrust_address(vpn.get("access_address"))
        start_type = str(vpn.get("start_type", "autostart")).strip().lower()
        if start_type not in self.ATRUST_START_TYPES:
            allowed = "、".join(sorted(self.ATRUST_START_TYPES))
            raise RemoteAccessError(f"aTrust start_type 仅支持：{allowed}")
        unsupported = [
            key
            for key in ("access_url", "username", "password", "interactive_login")
            if key in vpn
        ]
        if unsupported:
            keys = "、".join(unsupported)
            raise RemoteAccessError(
                f"aTrust 桌面客户端不会读取这些 JSON 字段：{keys}。"
                "请删除它们；首次认证由 aTrust 完成，后续使用已保存会话。"
            )
        try:
            ipaddress.IPv4Address(str(rdp.get("host", "")))
            ipaddress.IPv4Address(str(rdp.get("subnet_mask", "")))
        except ipaddress.AddressValueError as exc:
            raise RemoteAccessError("RDP host 或 subnet_mask 格式无效") from exc
        if not str(rdp.get("username", "")).strip():
            raise RemoteAccessError("RDP username 未配置")
        if not str(rdp.get("password", "")):
            raise RemoteAccessError("RDP password 未配置，请填写 config.json")
        if any(
            character in str(rdp.get("username", ""))
            for character in ("\r", "\n")
        ):
            raise RemoteAccessError("RDP username 不能包含换行符")
        proxy = config.get("socks_proxy") or {}
        if proxy:
            if not config.get("socks_over_rdp"):
                raise RemoteAccessError("socks_over_rdp 部署配置未配置")
            if not str(proxy.get("host", "")).strip():
                raise RemoteAccessError("socks_proxy.host 未配置")
            try:
                proxy_port = int(proxy.get("port", 1080))
                proxy_timeout = int(proxy.get("connect_timeout_seconds", 180))
            except (TypeError, ValueError) as exc:
                raise RemoteAccessError("socks_proxy 端口或超时时间格式无效") from exc
            if not 1 <= proxy_port <= 65535 or proxy_timeout < 1:
                raise RemoteAccessError("socks_proxy 端口或超时时间格式无效")

    @staticmethod
    def _ensure_socks_over_rdp(config: dict[str, Any]) -> None:
        proxy = config.get("socks_proxy") or {}
        if not proxy:
            return
        try:
            socks_over_rdp_installer.ensure(
                config.get("socks_over_rdp") or {},
                proxy,
            )
        except SocksOverRDPSetupError as exc:
            raise RemoteAccessError(str(exc)) from exc

    def _atrust_executable(self, vpn: dict[str, Any]) -> Path:
        configured = str(vpn.get("executable_path", "")).strip()
        candidates = [configured] if configured else list(self.ATRUST_CANDIDATES)
        for candidate in candidates:
            path = Path(candidate)
            if path.is_file():
                return path
        raise RemoteAccessError("未找到 aTrustTray.exe，请配置 vpn.executable_path")

    @staticmethod
    def _normalise_atrust_address(value: Any) -> str:
        address = str(value or "").strip()
        try:
            parsed = urlparse(address)
            port = parsed.port
        except ValueError as exc:
            raise RemoteAccessError("aTrust access_address 格式无效") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise RemoteAccessError(
                "aTrust access_address 必须是仅包含主机和可选端口的 HTTPS 地址"
            )
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = f":{port}" if port and port != 443 else ""
        return f"https://{host}{suffix}"

    @staticmethod
    def _atrust_address_file(vpn: dict[str, Any], executable: Path) -> Path:
        configured = str(vpn.get("address_file", "")).strip()
        if configured:
            return Path(configured)
        return executable.parent.parent / "aTrustAgent" / "var" / "conf" / "addr.conf"

    def _prepare_atrust_address(
        self, vpn: dict[str, Any], executable: Path
    ) -> None:
        """Provision aTrust's first-use address without opening its web portal."""
        address = self._normalise_atrust_address(vpn.get("access_address"))
        address_file = self._atrust_address_file(vpn, executable)
        current = ""
        try:
            if address_file.is_file():
                lines = address_file.read_text(encoding="utf-8").splitlines()
                current = lines[0].strip() if lines else ""
        except OSError as exc:
            raise RemoteAccessError(
                f"无法读取 aTrust 接入地址文件：{address_file}"
            ) from exc

        if current:
            try:
                if self._normalise_atrust_address(current) == address:
                    return
            except RemoteAccessError:
                pass

        try:
            address_file.write_text(address + "\n", encoding="utf-8")
        except OSError as exc:
            raise RemoteAccessError(
                "无法同步 aTrust 接入地址。请以管理员身份运行一次本程序，"
                f"或将 {address} 写入 {address_file}"
            ) from exc

    def _launch_atrust(self, vpn: dict[str, Any]) -> None:
        """Launch the desktop client and ask it to restore its retained session."""
        executable = self._atrust_executable(vpn)
        self._prepare_atrust_address(vpn, executable)
        start_type = str(vpn.get("start_type", "autostart")).strip().lower()
        subprocess.Popen(
            [str(executable), "--", "-s", start_type],
            cwd=str(executable.parent),
            close_fds=True,
        )

    @staticmethod
    def _port_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
        if not host:
            return False
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_for_port(
        self,
        host: str,
        port: int,
        timeout: int,
        purpose: str = "aTrust 连通",
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._port_reachable(host, port):
                return
            time.sleep(2)
        raise RemoteAccessError(
            f"等待 {purpose}超时：{timeout} 秒内无法访问 {host}:{port}"
        )

    @staticmethod
    def _write_rdp_credential(host: str, username: str, password: str) -> None:
        password_bytes = password.encode("utf-16-le")
        blob = (ctypes.c_ubyte * len(password_bytes)).from_buffer_copy(password_bytes)
        credential = CREDENTIALW(
            Flags=0,
            Type=2,
            TargetName=f"TERMSRV/{host}",
            Comment="Data Auto Analyzer temporary RDP credential",
            CredentialBlobSize=len(password_bytes),
            CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=1,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=username,
        )
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_write = advapi32.CredWriteW
        cred_write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        cred_write.restype = wintypes.BOOL
        if not cred_write(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise RemoteAccessError(f"写入临时 RDP 凭据失败，Windows 错误码 {error}")

    @staticmethod
    def _rdp_drive_redirect_value(socks_over_rdp: dict[str, Any]) -> str:
        install_dir = PureWindowsPath(
            str(socks_over_rdp.get("install_dir", "")).strip()
        )
        drive = install_dir.drive
        if len(drive) != 2 or drive[1] != ":":
            raise RemoteAccessError(
                "socks_over_rdp.install_dir 必须位于可重定向的本机盘符"
            )
        return f"{drive}\\;"

    @classmethod
    def _write_rdp_file(
        cls,
        rdp: dict[str, Any],
        socks_over_rdp: dict[str, Any],
    ) -> Path:
        host = str(rdp["host"])
        port = int(rdp.get("port", 3389))
        username = str(rdp["username"])
        redirected_drives = cls._rdp_drive_redirect_value(socks_over_rdp)
        rdp_directory = Path("./runtime/remote_access").resolve()
        rdp_directory.mkdir(parents=True, exist_ok=True)
        rdp_path = rdp_directory / f"{host.replace('.', '_')}_{port}.rdp"
        content = "\r\n".join(
            (
                f"full address:s:{host}:{port}",
                f"username:s:{username}",
                "prompt for credentials:i:0",
                "enablecredsspsupport:i:1",
                # Keep certificate failures visible instead of silently accepting them.
                "authentication level:i:2",
                "autoreconnection enabled:i:1",
                "redirectdrives:i:1",
                f"drivestoredirect:s:{redirected_drives}",
                "redirectclipboard:i:1",
            )
        ) + "\r\n"
        try:
            rdp_path.write_text(content, encoding="utf-16")
        except OSError as exc:
            raise RemoteAccessError(f"无法生成 RDP 连接文件：{rdp_path}") from exc
        return rdp_path

    def _launch_rdp(
        self,
        rdp: dict[str, Any],
        socks_over_rdp: dict[str, Any],
    ) -> None:
        host = str(rdp["host"])
        self._write_rdp_credential(
            host,
            str(rdp["username"]),
            str(rdp["password"]),
        )
        rdp_path = self._write_rdp_file(rdp, socks_over_rdp)
        executable = str(rdp.get("executable_path") or "mstsc.exe")
        subprocess.Popen([executable, str(rdp_path)], close_fds=True)


remote_access_service = RemoteAccessService()
