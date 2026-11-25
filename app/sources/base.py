from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from threading import Lock

from app.core.cancellation import cancellable_lock
from app.core.models import SourceItem


_browser_profiles = {}
_browser_profiles_guard = Lock()


def lock_browser_profile(profile: Path):
    """Serialize Chromium contexts using the same login profile."""
    with _browser_profiles_guard:
        lock = _browser_profiles.setdefault(profile, Lock())
    return cancellable_lock(lock)


class NetworkUnavailable(RuntimeError):
    """The source's network prerequisites are missing."""


class NoSourceRecords(RuntimeError):
    """A source successfully queried a day with no usable records."""


class DataSource(ABC):
    parallel_image_downloads = False
    image_cache_revision = ""
    network_button_label = ""

    def __init__(self, project_config: dict):
        self.config = project_config

    @abstractmethod
    def check_available(self) -> bool:
        """Return True when the source route is ready."""
        raise NotImplementedError

    @abstractmethod
    def fetch_day(self, business_date: date) -> list[SourceItem]:
        """Fetch one day's record metadata into SourceItem objects."""
        raise NotImplementedError

    def materialize_image(self, image_url: str, destination: Path) -> Path:
        """Optionally cache a remote image. Project sources may override this."""
        raise NotImplementedError("This source does not support remote image materialization")
