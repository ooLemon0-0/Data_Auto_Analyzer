from __future__ import annotations

from datetime import datetime, timedelta
import logging
import posixpath
import re
import shlex
from threading import RLock
import time

try:
    import paramiko
except ImportError:  # Allows the platform to start and report a useful setup error.
    paramiko = None

from app.diagnostics.models import LogChunk
from app.diagnostics.sources.base import LogSource

logger = logging.getLogger("data_review_platform.diagnostics.ssh")


class LogSourceError(RuntimeError):
    """Safe, user-readable log source failure."""


class SSHFileLogSource(LogSource):
    """Read only the requested inherited-timestamp window on the remote host."""

    _cache_lock = RLock()
    _chunk_cache: list[dict] = []

    def _cache_identity(self, path: str) -> tuple:
        return (
            self.config.get("host"),
            int(self.config.get("port", 22)),
            self.config.get("username"),
            path,
        )

    def _cached_chunk(
        self, path: str, start_time: datetime, end_time: datetime
    ) -> LogChunk | None:
        now = time.monotonic()
        identity = self._cache_identity(path)
        with self._cache_lock:
            self._chunk_cache[:] = [entry for entry in self._chunk_cache if entry["expires"] > now]
            for entry in reversed(self._chunk_cache):
                if (
                    entry["identity"] == identity
                    and entry["start"] <= start_time
                    and entry["end"] >= end_time
                ):
                    logger.info(
                        "Log diagnostics cache hit: host=%s path=%s range=%s..%s",
                        self.config.get("host"),
                        path,
                        entry["start"].isoformat(timespec="seconds"),
                        entry["end"].isoformat(timespec="seconds"),
                    )
                    return LogChunk(
                        source_name=entry["source_name"],
                        requested_start=start_time,
                        requested_end=end_time,
                        raw_text=entry["raw_text"],
                        remote_path=entry["remote_path"],
                    )
        return None

    def _store_chunk_cache(
        self,
        path: str,
        start_time: datetime,
        end_time: datetime,
        raw_text: str,
        remote_path: str,
    ) -> None:
        log_cfg = self.config.get("log", {})
        ttl = int(log_cfg.get("cache_ttl_seconds", 300))
        max_entries = int(log_cfg.get("cache_max_entries", 12))
        if ttl <= 0 or max_entries <= 0:
            return
        entry = {
            "identity": self._cache_identity(path),
            "start": start_time,
            "end": end_time,
            "raw_text": raw_text,
            "remote_path": remote_path,
            "source_name": f"ssh_file:{self.config['host']}",
            "expires": time.monotonic() + ttl,
        }
        with self._cache_lock:
            self._chunk_cache.append(entry)
            self._chunk_cache[:] = self._chunk_cache[-max_entries:]

    def _password(self) -> str:
        configured = str(self.config.get("auth", {}).get("password", ""))
        if configured:
            return configured
        location = "diagnostics.stations.<station>.source.auth.password"
        raise LogSourceError(f"SSH 密码未配置：请填写 {location}")

    def _connect(self):
        if paramiko is None:
            raise LogSourceError("缺少 paramiko，请执行: python -m pip install -r requirements.txt")
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.config.get("allow_unknown_host", False):
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # Resolve configuration before the connection try block so a useful
        # configuration error is not mislabeled as a network failure.
        password = self._password()
        try:
            client.connect(
                hostname=self.config["host"], port=int(self.config.get("port", 22)),
                username=self.config["username"], password=password,
                timeout=float(self.config.get("connect_timeout_seconds", 10)),
                banner_timeout=float(self.config.get("connect_timeout_seconds", 10)),
                auth_timeout=float(self.config.get("connect_timeout_seconds", 10)),
                look_for_keys=False, allow_agent=False,
            )
            return client
        except paramiko.AuthenticationException as exc:
            client.close()
            raise LogSourceError("现场日志 SSH 认证失败") from exc
        except LogSourceError:
            client.close()
            raise
        except Exception as exc:
            client.close()
            raise LogSourceError(f"无法连接现场盒子 {self.config.get('host', '')}") from exc

    def check_available(self) -> bool:
        try:
            self._connect().close()
            return True
        except LogSourceError:
            return False

    def _empty_window_diagnosis(
        self,
        client,
        path: str,
        start_time: datetime,
        end_time: datetime,
        searched_paths: list[str] | None = None,
    ) -> str:
        """Collect bounded metadata when the remote time-window filter is empty."""
        details = [
            "时间范围内没有日志",
            f"请求窗口: {start_time.isoformat(sep=' ', timespec='seconds')} ～ "
            f"{end_time.isoformat(sep=' ', timespec='seconds')}",
            f"当前日志: {path}",
            f"已筛选文件: {', '.join(searched_paths or [path])}",
        ]

        try:
            sftp = client.open_sftp()
            try:
                active = sftp.stat(path)
                details.append(
                    f"当前日志状态: {active.st_size} bytes"
                )

                directory = posixpath.dirname(path) or "."
                basename = posixpath.basename(path)
                candidates = [
                    entry
                    for entry in sftp.listdir_attr(directory)
                    if entry.filename == basename or entry.filename.startswith(f"{basename}.")
                ]
                def rotation_number(entry):
                    match = re.fullmatch(rf"{re.escape(basename)}\.(\d+)", entry.filename)
                    return int(match.group(1)) if match else 10**9

                rotated = sorted(
                    [entry for entry in candidates if entry.filename != basename],
                    key=rotation_number,
                )[:6]
                if rotated:
                    summary = "; ".join(
                        f"{entry.filename} ({entry.st_size} bytes)"
                        for entry in rotated
                    )
                    details.append(f"发现轮转/备份日志: {summary}")
                else:
                    details.append("同目录未发现以当前日志文件名开头的轮转文件")
            finally:
                sftp.close()
        except Exception as exc:
            details.append(f"读取日志文件元数据失败: {type(exc).__name__}")

        # Read only a bounded tail to determine whether the configured timestamp
        # protocol still matches the live log. This never downloads the full file.
        sample_path = (searched_paths or [path])[0]
        tail_command = f"tail -n 2000 -- {shlex.quote(sample_path)}"
        try:
            _, stdout, _ = client.exec_command(
                tail_command,
                timeout=float(self.config.get("command_timeout_seconds", 20)),
            )
            tail = stdout.read(1024 * 1024).decode(
                self.config.get("log", {}).get("encoding", "utf-8"),
                errors="replace",
            )
            timestamp_pattern = re.compile(
                r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}"
            )
            timestamps = timestamp_pattern.findall(tail)
            if timestamps:
                details.append(
                    f"{sample_path} 尾部可识别时间范围: "
                    f"{timestamps[0]} ～ {timestamps[-1]}（最近 2000 行）"
                )
            elif tail.strip():
                details.append(
                    "日志尾部有内容，但最近 2000 行未识别到 "
                    "YYYY-MM-DD HH:MM:SS 或 YYYY/MM/DD HH:MM:SS 时间戳；"
                    "可能是日志格式已经变化"
                )
            else:
                details.append("当前日志文件尾部为空")
        except Exception as exc:
            details.append(f"读取日志尾部样本失败: {type(exc).__name__}")

        return "；".join(details)

    def _select_log_paths(
        self,
        client,
        path: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[str]:
        """Select rotations by timestamps sampled from each file's head and tail."""
        directory = posixpath.dirname(path) or "."
        basename = posixpath.basename(path)
        rotated_pattern = re.compile(rf"^{re.escape(basename)}\.(\d+)$")
        try:
            sftp = client.open_sftp()
            try:
                sftp.stat(path)
                records: list[tuple[int, str]] = [(0, path)]
                for entry in sftp.listdir_attr(directory):
                    match = rotated_pattern.match(entry.filename)
                    if match:
                        records.append(
                            (
                                int(match.group(1)),
                                posixpath.join(directory, entry.filename),
                            )
                        )
            finally:
                sftp.close()
        except FileNotFoundError as exc:
            raise LogSourceError(f"日志文件不存在: {path}") from exc
        except OSError as exc:
            raise LogSourceError(f"无法读取日志目录或文件元数据: {path}") from exc

        records.sort(key=lambda record: record[0])
        sample_lines = int(self.config.get("log", {}).get("boundary_sample_lines", 80))
        selected: list[tuple[int, str]] = []
        ranges: list[str] = []
        for rotation, candidate_path in records:
            command = (
                f"head -n {sample_lines} -- {shlex.quote(candidate_path)}; "
                f"tail -n {sample_lines} -- {shlex.quote(candidate_path)}"
            )
            try:
                _, stdout, _ = client.exec_command(
                    command,
                    timeout=float(self.config.get("command_timeout_seconds", 20)),
                )
                sample = stdout.read(2 * 1024 * 1024).decode(
                    self.config.get("log", {}).get("encoding", "utf-8"),
                    errors="replace",
                )
            except Exception as exc:
                ranges.append(f"{candidate_path}: 边界读取失败({type(exc).__name__})")
                continue
            values = re.findall(
                r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}", sample
            )
            if not values:
                ranges.append(f"{candidate_path}: 头尾未识别到时间戳")
                continue
            lower = datetime.fromisoformat(values[0].replace("/", "-"))
            upper = datetime.fromisoformat(values[-1].replace("/", "-"))
            ranges.append(
                f"{candidate_path}: {lower.isoformat(sep=' ')} ～ {upper.isoformat(sep=' ')}"
            )
            overlaps = start_time <= upper and end_time >= lower
            if overlaps:
                selected.append((rotation, candidate_path))
                # Continue once because a query can cross the boundary into the
                # immediately older file. Once that neighbour does not overlap,
                # all remaining files are older and cannot match.
                continue
            if selected:
                break
            if start_time > upper:
                # Query is newer than this file. Files that follow are older.
                break

        # Return chronological order when a query crosses a rotation boundary.
        selected.sort(key=lambda item: item[0], reverse=True)
        if selected:
            return [candidate_path for _, candidate_path in selected]
        raise LogSourceError(
            "查询窗口不在已识别的日志文件时间范围内；请求窗口: "
            f"{start_time.isoformat(sep=' ', timespec='seconds')} ～ "
            f"{end_time.isoformat(sep=' ', timespec='seconds')}；"
            + "；".join(ranges)
        )

    def fetch(self, start_time: datetime, end_time: datetime) -> LogChunk:
        log_cfg = self.config["log"]
        path = str(log_cfg["path"])
        max_bytes = int(log_cfg.get("max_output_bytes", 8 * 1024 * 1024))
        cached = self._cached_chunk(path, start_time, end_time)
        if cached is not None:
            return cached

        cache_window = max(0, int(log_cfg.get("cache_window_seconds", 180)))
        fetch_start = start_time - timedelta(seconds=cache_window)
        fetch_end = end_time + timedelta(seconds=cache_window)
        # Timestamp-bearing lines update ts; all following protocol lines inherit it.
        awk = (
            r'match($0, /[0-9][0-9][0-9][0-9][-\/][0-9][0-9][-\/][0-9][0-9][ T][0-9][0-9]:[0-9][0-9]:[0-9][0-9]/) '
            r'{ s=substr($0,RSTART,RLENGTH); gsub("/","-",s); ts=s; if (ts > finish) exit } '
            r'ts >= start && ts <= finish { print }'
        )
        client = self._connect()
        try:
            selected_paths = self._select_log_paths(client, path, fetch_start, fetch_end)
            chunks: list[bytes] = []
            total_bytes = 0
            for selected_path in selected_paths:
                command = "awk -v start={0} -v finish={1} {2} {3}".format(
                    shlex.quote(fetch_start.strftime("%Y-%m-%d %H:%M:%S")),
                    shlex.quote(fetch_end.strftime("%Y-%m-%d %H:%M:%S")),
                    shlex.quote(awk),
                    shlex.quote(selected_path),
                )
                _, stdout, stderr = client.exec_command(
                    command, timeout=float(self.config.get("command_timeout_seconds", 20))
                )
                raw_part = stdout.read(max_bytes - total_bytes + 1)
                error = stderr.read(4096).decode("utf-8", errors="replace").strip()
                status = stdout.channel.recv_exit_status()
                if status != 0:
                    if "No such file" in error:
                        raise LogSourceError(f"日志文件不存在: {selected_path}")
                    raise LogSourceError(
                        f"远程日志读取失败 ({selected_path}): {error or 'unknown error'}"
                    )
                total_bytes += len(raw_part)
                if total_bytes > max_bytes:
                    raise LogSourceError(
                        f"时间窗口日志超过安全上限 {max_bytes} bytes，请缩小窗口"
                    )
                chunks.append(raw_part)

            raw = b"\n".join(chunks)
            text = raw.decode(log_cfg.get("encoding", "utf-8"), errors="replace")
            if not text.strip():
                raise LogSourceError(
                    self._empty_window_diagnosis(
                        client, path, start_time, end_time, selected_paths
                    )
                )
            remote_path = ", ".join(selected_paths)
            self._store_chunk_cache(
                path, fetch_start, fetch_end, text, remote_path
            )
            return LogChunk(
                source_name=f"ssh_file:{self.config['host']}",
                requested_start=start_time,
                requested_end=end_time,
                raw_text=text,
                remote_path=remote_path,
            )
        finally:
            client.close()
