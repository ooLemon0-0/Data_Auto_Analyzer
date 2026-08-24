from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class CoordinateSpace(str, Enum):
    ORIGINAL_IMAGE = "original_image"
    SURFACE_ROI = "surface_roi"
    DESKEW_IMAGE = "deskew_image"
    CLS_CROP = "cls_crop"


class Point(BaseModel):
    x: float
    y: float


class BoxXYXY(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    coordinate_space: CoordinateSpace


class Polygon(BaseModel):
    points: list[Point] = Field(default_factory=list)
    coordinate_space: CoordinateSpace


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


class SurfaceDiagnostic(BaseModel):
    signal: int | None = None
    total: int | None = None
    trigger_box: BoxXYXY | None = None
    roi_box: BoxXYXY | None = None
    image_width: int | None = None
    image_height: int | None = None


class DetBoxDiagnostic(BaseModel):
    index: int
    raw_polygon: Polygon
    original_polygon: Polygon | None = None
    filtered: bool = False


class DeskewBoxDiagnostic(BaseModel):
    box_index: int
    angle: float | None = None
    aspect: float | None = None
    long_side: float | None = None
    weight: float | None = None


class DeskewDiagnostic(BaseModel):
    boxes: list[DeskewBoxDiagnostic] = Field(default_factory=list)
    final_angle: float | None = None
    raw_estimate: float | None = None
    consistency: float | None = None
    conflict: bool | None = None
    method: str | None = None


class CLSBoxDiagnostic(BaseModel):
    box_index: int
    original_p0: float | None = None
    original_p180: float | None = None
    rotated_p0: float | None = None
    rotated_p180: float | None = None
    dual_margin: float | None = None
    evidence: float | None = None
    individual_rotate180: bool | None = None
    is_reverse: bool | None = None
    crop_width: int | None = None
    crop_height: int | None = None


class CLSGlobalDiagnostic(BaseModel):
    evidence_sum: float | None = None
    evidence_abs_sum: float | None = None
    consensus: bool | int | None = None
    positive: int | None = None
    negative: int | None = None
    zero: int | None = None
    conflict: bool | None = None
    anchor_box: int | None = None
    anchor_evidence: float | None = None
    method: str | None = None
    uncertain: bool | None = None
    overrides: int | None = None
    boxes: int | None = None
    rotate180: bool | None = None


class OCRStringDiagnostic(BaseModel):
    index: int
    text: str
    length: int | None = None
    rec_method: int | None = None
    polygon: Polygon | None = None


class OCRDiagnostic(BaseModel):
    success: bool | None = None
    message: str | None = None
    strings: list[OCRStringDiagnostic] = Field(default_factory=list)
    full_picture_url: str | None = None
    joined_text: str | None = None


class DiagnosticEvent(BaseModel):
    event_id: str | None = None
    start_time: datetime | None = None
    trigger_time: datetime | None = None
    finish_time: datetime | None = None
    image_name: str | None = None
    image_url: str | None = None
    surface: SurfaceDiagnostic | None = None
    det_boxes: list[DetBoxDiagnostic] = Field(default_factory=list)
    deskew: DeskewDiagnostic | None = None
    cls_boxes: list[CLSBoxDiagnostic] = Field(default_factory=list)
    cls_global: CLSGlobalDiagnostic | None = None
    ocr: OCRDiagnostic | None = None
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
    overlays: list[RenderOverlay] = Field(default_factory=list)
