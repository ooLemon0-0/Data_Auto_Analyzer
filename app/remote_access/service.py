from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
from threading import RLock, Thread
import time
from typing import Any
from urllib.parse import urlparse
import webbrowser

from app.core.config import settings


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

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _set_status(self, connection_id: str, **values: Any) -> None:
        with self._lock:
            current = self._jobs.setdefault(connection_id, {})
            current.update(values, updated_at=self._now())

    def status(self, connection_id: str) -> dict[str, Any]:
        config = settings.remote_connection(connection_id)
        rdp = config.get("rdp", {})
        host, port = str(rdp.get("host", "")), int(rdp.get("port", 3389))
        proxy = config.get("socks_proxy") or {}
        proxy_host = str(proxy.get("host", "")).strip()
        proxy_port = int(proxy.get("port", 1080))
        with self._lock:
            job = dict(self._jobs.get(connection_id, {}))
        return {
            "connection_id": connection_id,
            "name": config.get("name", connection_id),
            "phase": job.get("phase", "idle"),
            "message": job.get("message", "尚未启动"),
            "started_at": job.get("started_at"),
            "updated_at": job.get("updated_at"),
            "rdp_target": f"{host}:{port}",
            "rdp_reachable": self._port_reachable(host, port, timeout=0.4),
            "proxy_target": f"{proxy_host}:{proxy_port}" if proxy_host else None,
            "proxy_reachable": (
                self._port_reachable(proxy_host, proxy_port, timeout=0.2)
                if proxy_host
                else None
            ),
        }

    def launch(self, connection_id: str) -> dict[str, Any]:
        config = settings.remote_connection(connection_id)
        self._validate(config)
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
                message="请在 aTrust 中完成登录；正在等待远程桌面网络可达",
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
            self._launch_rdp(rdp)
            proxy = config.get("socks_proxy") or {}
            proxy_host = str(proxy.get("host", "")).strip()
            if proxy_host:
                proxy_port = int(proxy.get("port", 1080))
                proxy_timeout = int(proxy.get("connect_timeout_seconds", 180))
                self._set_status(
                    connection_id,
                    phase="waiting_proxy",
                    message="远程桌面已启动，正在等待 SocksOverRDP 代理",
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
            self._set_status(connection_id, phase="failed", message=str(exc))

    def _validate(self, config: dict[str, Any]) -> None:
        if os.name != "nt":
            raise RemoteAccessError("aTrust/RDP 链路仅支持 Windows")
        if not config.get("enabled", True):
            raise RemoteAccessError("该远程连接已禁用")
        vpn = config.get("vpn") or {}
        rdp = config.get("rdp") or {}
        if vpn.get("type") != "atrust":
            raise RemoteAccessError("remote connection 的 vpn.type 必须是 atrust")
        parsed = urlparse(str(vpn.get("access_url", "")))
        if parsed.scheme != "https" or not parsed.hostname:
            raise RemoteAccessError("aTrust access_url 必须是有效的 HTTPS 地址")
        if not str(vpn.get("username", "")).strip():
            raise RemoteAccessError("aTrust username 未配置")
        try:
            ipaddress.IPv4Address(str(rdp.get("host", "")))
            ipaddress.IPv4Address(str(rdp.get("subnet_mask", "")))
        except ipaddress.AddressValueError as exc:
            raise RemoteAccessError("RDP host 或 subnet_mask 格式无效") from exc
        if not str(rdp.get("username", "")).strip():
            raise RemoteAccessError("RDP username 未配置")
        if not str(rdp.get("password", "")):
            raise RemoteAccessError("RDP password 未配置，请填写 config.json")
        proxy = config.get("socks_proxy") or {}
        if proxy:
            if not str(proxy.get("host", "")).strip():
                raise RemoteAccessError("socks_proxy.host 未配置")
            try:
                proxy_port = int(proxy.get("port", 1080))
                proxy_timeout = int(proxy.get("connect_timeout_seconds", 180))
            except (TypeError, ValueError) as exc:
                raise RemoteAccessError("socks_proxy 端口或超时时间格式无效") from exc
            if not 1 <= proxy_port <= 65535 or proxy_timeout < 1:
                raise RemoteAccessError("socks_proxy 端口或超时时间格式无效")

    def _atrust_executable(self, vpn: dict[str, Any]) -> Path:
        configured = str(vpn.get("executable_path", "")).strip()
        candidates = [configured] if configured else list(self.ATRUST_CANDIDATES)
        for candidate in candidates:
            path = Path(candidate)
            if path.is_file():
                return path
        raise RemoteAccessError("未找到 aTrustTray.exe，请配置 vpn.executable_path")

    def _launch_atrust(self, vpn: dict[str, Any]) -> None:
        executable = self._atrust_executable(vpn)
        subprocess.Popen([str(executable)], close_fds=True)
        time.sleep(float(vpn.get("launch_settle_seconds", 2)))
        if not webbrowser.open(str(vpn["access_url"]), new=2):
            raise RemoteAccessError("aTrust 已启动，但无法打开接入地址")

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

    def _launch_rdp(self, rdp: dict[str, Any]) -> None:
        host = str(rdp["host"])
        port = int(rdp.get("port", 3389))
        self._write_rdp_credential(
            host,
            str(rdp["username"]),
            str(rdp["password"]),
        )
        executable = str(rdp.get("executable_path") or "mstsc.exe")
        subprocess.Popen([executable, f"/v:{host}:{port}"], close_fds=True)


remote_access_service = RemoteAccessService()
