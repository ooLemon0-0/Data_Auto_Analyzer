from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any


class DataSink(ABC):
    def __init__(self, project_config: dict):
        self.config = project_config

    def prepare_auth(self) -> dict[str, Any]:
        """Optional interactive authentication/preflight step for browser-backed sinks."""
        return {"authenticated": True, "reason": "sink does not require interactive auth"}

    @abstractmethod
    def upload_day(self, business_date: date, summary: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
