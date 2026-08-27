from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Point(BaseModel):
    x: float
    y: float


class BoxXYXY(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    coordinate_space: str


class Polygon(BaseModel):
    points: list[Point] = Field(default_factory=list)
    coordinate_space: str


class LogChunk(BaseModel):
    source_name: str
    requested_start: datetime
    requested_end: datetime
    raw_text: str
    remote_path: str | None = None


class DiagnosticQuery(BaseModel):
    project_id: str
    event_time: datetime
    expected_result: str | None = None
    station: str | None = None
    image_name: str | None = None


class DiagnosticSearchQuery(BaseModel):
    project_id: str
    start_time: datetime
    end_time: datetime
    expected_result: str | None = None
    station: str | None = None
    image_name: str | None = None


class DiagnosticEvent(BaseModel):
    event_id: str | None = None
    start_time: datetime | None = None
    trigger_time: datetime | None = None
    finish_time: datetime | None = None
    image_name: str | None = None
    image_url: str | None = None
    details: Any = None
    raw_log: str = ""
    warnings: list[str] = Field(default_factory=list)


class DiagnosticResult(BaseModel):
    matched: bool
    match_score: float = 0.0
    parser_type: str
    query: DiagnosticQuery
    event: DiagnosticEvent | None = None
    warnings: list[str] = Field(default_factory=list)
    alternatives: list[DiagnosticEvent] = Field(default_factory=list)


class RenderOverlay(BaseModel):
    role: str
    label: str
    state: str | None = None
    box: BoxXYXY | None = None
    polygon: Polygon | None = None


class DiagnosticRenderPayload(BaseModel):
    result: DiagnosticResult
    image_width: int | None = None
    image_height: int | None = None
    overlays: list[RenderOverlay] = Field(default_factory=list)


class DiagnosticSearchMatch(BaseModel):
    source_name: str
    payload: DiagnosticRenderPayload


class DiagnosticSearchResult(BaseModel):
    matches: list[DiagnosticSearchMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
