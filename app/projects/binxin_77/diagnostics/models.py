from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.diagnostics.models import BoxXYXY, Polygon


class CoordinateSpace(str, Enum):
    ORIGINAL_IMAGE = "original_image"
    SURFACE_ROI = "surface_roi"
    DESKEW_IMAGE = "deskew_image"
    CLS_CROP = "cls_crop"


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


class BinxinDiagnosticData(BaseModel):
    surface: SurfaceDiagnostic | None = None
    det_boxes: list[DetBoxDiagnostic] = Field(default_factory=list)
    deskew: DeskewDiagnostic | None = None
    cls_boxes: list[CLSBoxDiagnostic] = Field(default_factory=list)
    cls_global: CLSGlobalDiagnostic | None = None
    ocr: OCRDiagnostic | None = None




