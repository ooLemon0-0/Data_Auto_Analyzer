from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.diagnostics.models import LogChunk


class LogSource(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def check_available(self) -> bool: ...

    @abstractmethod
    def fetch(self, start_time: datetime, end_time: datetime) -> LogChunk: ...
