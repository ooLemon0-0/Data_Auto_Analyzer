from __future__ import annotations

from abc import ABC, abstractmethod

from app.diagnostics.models import DiagnosticResult


class DiagnosticResolver(ABC):
    """Project-owned policy for deciding whether another log source is needed."""

    resolver_type: str

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def should_fallback(self, result: DiagnosticResult, fallback: dict) -> bool:
        """Return True when this result should be replaced by a later source."""
        raise NotImplementedError

