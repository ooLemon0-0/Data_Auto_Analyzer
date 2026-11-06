from __future__ import annotations

import csv
import os
from pathlib import Path
import subprocess

import requests
from app.core.cancellation import check_cancelled
from app.core.http import http_get


class EasyConnectManager:
    PROCESS_NAMES = {"sangforcsclient.exe", "easyconnect.exe"}

    @staticmethod
    def candidates() -> list[Path]:
        paths = []
        for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                for suffix in ("Sangfor/SSL/SangforCSClient/SangforCSClient.exe",
                               "Sangfor/AF/SSL/SangforCSClient/SangforCSClient.exe",
                               "Sangfor/EasyConnect/EasyConnect.exe"):
                    paths.append(Path(root) / suffix)
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key_name in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SangforCSClient.exe",
                                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\EasyConnect.exe"):
                    try:
                        with winreg.OpenKey(hive, key_name) as key:
                            paths.append(Path(winreg.QueryValue(key, None).strip('"')))
                    except OSError:
                        pass
        except ImportError:
            pass
        return paths

    @staticmethod
    def process_names() -> set[str]:
        if os.name != "nt":
            return set()
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {row[0].lower() for row in csv.reader(result.stdout.splitlines()) if row}

    def ensure(self, healthcheck_url: str) -> dict:
        check_cancelled()
        running = bool(self.PROCESS_NAMES & self.process_names())
        executable = next((path for path in self.candidates() if path.is_file()), None)
        started = False
        if not running and executable:
            check_cancelled()
            # This is the VPN's interactive window, where the user may need to log in.
            subprocess.Popen([str(executable)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = True
        try:
            available = http_get(healthcheck_url, timeout=3).status_code < 500
        except requests.RequestException:
            available = False
        if available:
            message = "内网已连通"
        elif started or running:
            message = "EasyConnect 已启动，请完成 VPN 登录"
        else:
            message = "未找到 EasyConnect，请先安装企业 VPN 客户端"
        return {"available": available, "running": running or started, "started": started,
                "installed": executable is not None, "message": message}
