from datetime import datetime
from types import SimpleNamespace

from app.diagnostics.sources.ssh_file import SSHFileLogSource


class _Stream:
    def __init__(self, value: bytes):
        self.value = value

    def read(self, _limit=None):
        return self.value


class _SFTP:
    def stat(self, _path):
        return SimpleNamespace(st_size=1200, st_mtime=1_777_000_000)

    def listdir_attr(self, _directory):
        return [
            SimpleNamespace(filename="app.log", st_size=1200, st_mtime=1_777_000_000),
            SimpleNamespace(filename="app.log.1", st_size=5000, st_mtime=1_776_999_000),
            SimpleNamespace(filename="unrelated.log", st_size=8, st_mtime=1_776_998_000),
        ]

    def close(self):
        pass


class _Client:
    def __init__(self, tail: bytes):
        self.tail = tail

    def open_sftp(self):
        return _SFTP()

    def exec_command(self, _command, timeout=None):
        return None, _Stream(self.tail), _Stream(b"")


class _RotationSFTP:
    def __init__(self):
        self.times = {
            "app.log": datetime(2026, 8, 24, 22, 28, 45).timestamp(),
            "app.log.1": datetime(2026, 8, 24, 22, 15, 53).timestamp(),
            "app.log.2": datetime(2026, 8, 23, 6, 44, 22).timestamp(),
            "app.log.3": datetime(2026, 8, 21, 15, 17, 25).timestamp(),
        }

    def stat(self, path):
        return SimpleNamespace(st_size=100, st_mtime=self.times[path.rsplit("/", 1)[-1]])

    def listdir_attr(self, _directory):
        return [
            SimpleNamespace(filename=name, st_size=100, st_mtime=mtime)
            for name, mtime in self.times.items()
        ] + [SimpleNamespace(filename="app.log.backup", st_size=100, st_mtime=0)]

    def close(self):
        pass


class _RotationClient:
    def open_sftp(self):
        return _RotationSFTP()

    def exec_command(self, command, timeout=None):
        ranges = {
            "app.log.1": b"[2026-08-23 06:44:23] first\n[2026-08-24 22:15:53] last\n",
            "app.log.2": b"[2026-08-21 15:17:26] first\n[2026-08-23 06:44:22] last\n",
            "app.log.3": b"[2026-08-19 23:46:05] first\n[2026-08-21 15:17:25] last\n",
            "app.log": b"[2026-08-24 22:15:54] first\n[2026-08-24 22:28:45] last\n",
        }
        name = next(
            key for key in ("app.log.1", "app.log.2", "app.log.3", "app.log")
            if key in command
        )
        return None, _Stream(ranges[name]), _Stream(b"")


def test_empty_window_reports_file_rotation_and_tail_range():
    source = SSHFileLogSource({"log": {"encoding": "utf-8"}})
    message = source._empty_window_diagnosis(
        _Client(b"[2026-08-24 10:00:00] first\n[2026-08-24 10:30:00] last\n"),
        "/logs/app.log",
        datetime(2026, 8, 24, 9, 0, 0),
        datetime(2026, 8, 24, 9, 1, 0),
    )
    assert "请求窗口" in message
    assert "app.log.1" in message
    assert "2026-08-24 10:00:00 ～ 2026-08-24 10:30:00" in message


def test_empty_window_identifies_changed_timestamp_format():
    source = SSHFileLogSource({"log": {"encoding": "utf-8"}})
    message = source._empty_window_diagnosis(
        _Client(b"Aug 24 10:30:00 protocol changed\n"),
        "/logs/app.log",
        datetime(2026, 8, 24, 9, 0, 0),
        datetime(2026, 8, 24, 9, 1, 0),
    )
    assert "日志格式已经变化" in message


def test_rotation_selection_uses_sampled_content_intervals():
    source = SSHFileLogSource({})
    client = _RotationClient()
    assert source._select_log_paths(
        client,
        "/logs/app.log",
        datetime(2026, 8, 24, 20, 34, 3),
        datetime(2026, 8, 24, 20, 34, 26),
    ) == ["/logs/app.log.1"]
    assert source._select_log_paths(
        client,
        "/logs/app.log",
        datetime(2026, 8, 24, 22, 20, 0),
        datetime(2026, 8, 24, 22, 20, 30),
    ) == ["/logs/app.log"]


def test_rotation_selection_reads_two_files_at_boundary():
    source = SSHFileLogSource({})
    paths = source._select_log_paths(
        _RotationClient(),
        "/logs/app.log",
        datetime(2026, 8, 24, 22, 15, 50),
        datetime(2026, 8, 24, 22, 16, 0),
    )
    assert paths == ["/logs/app.log.1", "/logs/app.log"]


def test_nearby_windows_reuse_in_memory_chunk_cache():
    source = SSHFileLogSource(
        {
            "host": "box-a",
            "port": 22,
            "username": "user",
            "log": {"cache_ttl_seconds": 300, "cache_max_entries": 4},
        }
    )
    source._chunk_cache.clear()
    source._store_chunk_cache(
        "/logs/app.log",
        datetime(2026, 8, 24, 20, 30, 0),
        datetime(2026, 8, 24, 20, 40, 0),
        "cached log text",
        "/logs/app.log.1",
    )
    cached = source._cached_chunk(
        "/logs/app.log",
        datetime(2026, 8, 24, 20, 34, 3),
        datetime(2026, 8, 24, 20, 34, 26),
    )
    assert cached is not None
    assert cached.raw_text == "cached log text"
    assert cached.remote_path == "/logs/app.log.1"


def test_cache_does_not_cross_host_or_uncovered_time():
    source = SSHFileLogSource(
        {"host": "box-b", "port": 22, "username": "user", "log": {}}
    )
    assert source._cached_chunk(
        "/logs/app.log",
        datetime(2026, 8, 24, 20, 34, 3),
        datetime(2026, 8, 24, 20, 34, 26),
    ) is None
    original_source = SSHFileLogSource(
        {"host": "box-a", "port": 22, "username": "user", "log": {}}
    )
    assert original_source._cached_chunk(
        "/logs/app.log",
        datetime(2026, 8, 24, 21, 0, 0),
        datetime(2026, 8, 24, 21, 0, 20),
    ) is None
