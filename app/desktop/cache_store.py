"""Only manage files under data/<project>/<ISO date>/images, never audit data."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import os
from pathlib import Path
import stat

from app.core.cancellation import check_cancelled


def format_bytes(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


@dataclass
class CacheDay:
    day: str
    files: int = 0
    size: int = 0
    projects: dict = field(default_factory=dict)


def _plain(path):
    try:
        info = path.lstat()
        return not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


class CacheStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    @staticmethod
    def _date(value):
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("请选择有效的缓存日期")
        return value

    def _image_roots(self, selected=None):
        if not self.root.is_dir():
            return
        for project in self.root.iterdir():
            check_cancelled()
            if not _plain(project) or not project.is_dir():
                continue
            for day in project.iterdir():
                try:
                    self._date(day.name)
                except ValueError:
                    continue
                if selected is not None and day.name not in selected:
                    continue
                images = day / "images"
                if not _plain(day) or not _plain(images) or not images.is_dir():
                    continue
                if images.resolve().is_relative_to(self.root):
                    yield project.name, day.name, images

    def _files(self, images):
        for folder, directories, names in os.walk(images, followlinks=False):
            check_cancelled()
            directories[:] = [name for name in directories if _plain(Path(folder) / name)]
            for name in names:
                path = Path(folder) / name
                if _plain(path) and path.resolve().is_relative_to(images.resolve()) and path.is_file():
                    try:
                        yield path, path.stat().st_size
                    except FileNotFoundError:
                        pass

    def scan(self):
        days = {}
        for project, day, images in self._image_roots():
            for _path, size in self._files(images):
                entry = days.setdefault(day, CacheDay(day))
                entry.files += 1
                entry.size += size
                info = entry.projects.setdefault(project, {"files": 0, "size": 0})
                info["files"] += 1
                info["size"] += size
        return sorted(days.values(), key=lambda entry: entry.day, reverse=True)

    def clear(self, days, progress=lambda *_args: None):
        selected = {self._date(day) for day in days}
        if not selected:
            raise ValueError("请先选择缓存日期")
        files = [(images, path, size) for _project, _day, images in self._image_roots(selected)
                 for path, size in self._files(images)]
        removed, freed, failures = 0, 0, []
        for index, (images, path, _size) in enumerate(files, 1):
            check_cancelled()
            # Recheck every ancestor immediately before deletion. Junctions and
            # symlinks must never turn a cache cleanup into an external delete.
            safe = all(_plain(parent) for parent in (images.parent.parent, images.parent, images))
            parent = path.parent
            while parent != images and parent.is_relative_to(images):
                safe = safe and _plain(parent)
                parent = parent.parent
            if not safe or not _plain(path) or not path.resolve().is_relative_to(images.resolve()):
                failures.append("发现外部链接，已跳过")
                continue
            try:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                freed += size
            except FileNotFoundError:
                pass
            except OSError:
                failures.append(path.name)
            if index == 1 or index % 64 == 0 or index == len(files):
                progress(index, len(files))
        return {"dates": sorted(selected), "removed": removed, "freed": freed, "failed": len(failures)}
