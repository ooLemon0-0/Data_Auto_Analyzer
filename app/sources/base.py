from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

from app.core.models import SourceItem


class DataSource(ABC):
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
