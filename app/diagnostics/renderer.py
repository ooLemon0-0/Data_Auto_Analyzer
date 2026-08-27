from __future__ import annotations

from abc import ABC, abstractmethod

from app.diagnostics.models import DiagnosticRenderPayload, DiagnosticResult


class DiagnosticRenderer(ABC):
    """Convert a project diagnostic result into the stable frontend payload."""

    renderer_type: str

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    def build(self, result: DiagnosticResult) -> DiagnosticRenderPayload:
        raise NotImplementedError

