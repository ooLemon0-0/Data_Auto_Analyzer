from __future__ import annotations

from abc import ABC, abstractmethod

from app.diagnostics.models import DiagnosticEvent, DiagnosticQuery


class LogParser(ABC):
    parser_type: str

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def parse_events(self, raw_text: str) -> list[DiagnosticEvent]: ...

    @abstractmethod
    def score_event(self, event: DiagnosticEvent, query: DiagnosticQuery) -> float: ...

    def select_event(self, events: list[DiagnosticEvent], query: DiagnosticQuery):
        ranked = sorted(((self.score_event(e, query), e) for e in events), key=lambda x: x[0], reverse=True)
        if not ranked:
            return None, 0.0, []
        return ranked[0][1], ranked[0][0], [event for _, event in ranked[1:4]]
