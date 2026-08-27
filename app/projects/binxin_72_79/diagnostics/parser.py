from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path
from typing import Any

from app.diagnostics.models import (
    BoxXYXY, DiagnosticEvent, DiagnosticQuery, Point, Polygon,
)
from app.diagnostics.parser import LogParser
from app.projects.binxin_72_79.diagnostics.models import (
    BinxinDiagnosticData, CLSBoxDiagnostic, CLSGlobalDiagnostic, CoordinateSpace,
    DeskewBoxDiagnostic, DeskewDiagnostic, DetBoxDiagnostic, OCRDiagnostic,
    OCRStringDiagnostic, SurfaceDiagnostic,
)


class Binxin7279LogParser(LogParser):
    """Tolerant parser for the Binxin OCR protocol markers."""

    parser_type = "binxin_72_79_ocr"
    START = "取到图片开始一次推理"
    END = "本次取图推理结束"
    TS = re.compile(r"(?P<ts>\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)")
    NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"

    def parse_events(self, raw_text: str) -> list[DiagnosticEvent]:
        blocks = self._split_events(raw_text)
        events: list[DiagnosticEvent] = []
        for index, block in enumerate(blocks):
            try:
                events.append(self._parse_event(block, index))
            except Exception as exc:
                # A malformed event must not hide valid neighbours.
                events.append(DiagnosticEvent(event_id=f"binxin-{index}", raw_log=block,
                                              warnings=[f"事件部分解析失败: {exc}"]))
        return [event for event in events if self.START in event.raw_log]

    def score_event(self, event: DiagnosticEvent, query: DiagnosticQuery) -> float:
        match = self.config.get("match", {})
        max_delta = float(match.get("max_time_difference_seconds", 20))
        time_weight = float(match.get("time_weight", 30))
        result_weight = float(match.get("result_weight", 50))
        image_weight = float(match.get("image_name_weight", 100))
        score = 0.0
        event_time = event.trigger_time or event.start_time
        if event_time:
            delta = abs((event_time - query.event_time).total_seconds())
            if delta <= max_delta:
                score += time_weight * (1 - delta / max(max_delta, 0.001))
        expected = self._normalise(query.expected_result)
        details = event.details if isinstance(event.details, BinxinDiagnosticData) else None
        actual = self._normalise(details.ocr.joined_text if details and details.ocr else None)
        if expected and actual and expected == actual:
            score += result_weight
        if query.image_name and event.image_name and Path(query.image_name).name == Path(event.image_name).name:
            score += image_weight
        return score

    @staticmethod
    def _normalise(value: str | None) -> str:
        return re.sub(r"\s+", "", value or "").upper()

    def _split_events(self, text: str) -> list[str]:
        result: list[str] = []
        current: list[str] | None = None
        pending: list[str] = []
        for line in text.splitlines():
            if self.START in line:
                if current:
                    result.append("\n".join(current))
                current = pending[-20:] + [line]
                pending = []
            elif current is not None:
                current.append(line)
                if self.END in line:
                    result.append("\n".join(current))
                    current = None
            else:
                pending.append(line)
        if current:
            result.append("\n".join(current))
        return result

    def _parse_event(self, block: str, index: int) -> DiagnosticEvent:
        timestamps = [self._datetime(m.group("ts")) for m in self.TS.finditer(block)]
        event = DiagnosticEvent(
            event_id=f"binxin-{index}", start_time=timestamps[0] if timestamps else None,
            trigger_time=self._marker_time(block, self.START),
            finish_time=self._marker_time(block, self.END) or (timestamps[-1] if timestamps else None),
            raw_log=block, details=BinxinDiagnosticData(),
        )
        details = event.details
        details.surface = self._parse_surface(block)
        details.det_boxes = self._parse_det(block, details.surface)
        details.deskew = self._parse_deskew(block)
        details.cls_boxes, details.cls_global = self._parse_cls(block)
        details.ocr = self._parse_ocr(block, event.warnings)
        if details.ocr:
            event.image_url = details.ocr.full_picture_url
            if event.image_url:
                event.image_name = Path(event.image_url.split("?", 1)[0]).name
        return event

    def _datetime(self, value: str) -> datetime:
        cleaned = value.replace("/", "-").replace(",", ".")
        return datetime.fromisoformat(cleaned)

    def _marker_time(self, block: str, marker: str) -> datetime | None:
        current = None
        for line in block.splitlines():
            match = self.TS.search(line)
            if match:
                current = self._datetime(match.group("ts"))
            if marker in line:
                return current
        return None

    @staticmethod
    def _json_objects_after(text: str, marker: str) -> list[dict]:
        objects: list[dict] = []
        cursor = 0
        while True:
            pos = text.find(marker, cursor)
            if pos < 0:
                return objects
            start = text.find("{", pos)
            if start < 0:
                return objects
            depth = 0
            quoted = escaped = False
            end = None
            for i in range(start, len(text)):
                char = text[i]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end is None:
                return objects
            try:
                objects.append(json.loads(text[start:end]))
            except json.JSONDecodeError:
                pass
            cursor = end

    def _parse_surface(self, block: str) -> SurfaceDiagnostic | None:
        payloads = self._json_objects_after(block, "[SURFACE-UPSTREAM]")
        # Later SURFACE-UPSTREAM protocol lines may be followed by the final
        # inference JSON. Select the actual upstream detList payload instead
        # of blindly taking the last JSON object after the marker.
        payload = next(
            (candidate for candidate in payloads if isinstance(candidate.get("detList"), list)),
            {},
        )
        det_list = payload.get("detList") or []
        coordinate = det_list[-1].get("detCoordinate") if det_list else None
        trigger = self._box(coordinate, CoordinateSpace.ORIGINAL_IMAGE)
        roi = None
        roi_match = re.search(r"\[SURFACE-ROI\][\s\S]{0,500}?\b(?:x|left)\s*=\s*(%s)[,\s]+(?:y|top)\s*=\s*(%s)[,\s]+(?:w|width)\s*=\s*(%s)[,\s]+(?:h|height)\s*=\s*(%s)" % ((self.NUMBER,) * 4), block, re.I)
        if roi_match:
            x, y, w, h = map(float, roi_match.groups())
            roi = BoxXYXY(x1=x, y1=y, x2=x + w, y2=y + h, coordinate_space=CoordinateSpace.ORIGINAL_IMAGE)
        else:
            roi_tuple = re.search(
                r"\[SURFACE-ROI\][^\r\n]*?\broi\s*=\s*\(\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*\)"
                % ((self.NUMBER,) * 4),
                block,
                re.I,
            )
            if roi_tuple:
                x, y, w, h = map(float, roi_tuple.groups())
                roi = BoxXYXY(
                    x1=x, y1=y, x2=x + w, y2=y + h,
                    coordinate_space=CoordinateSpace.ORIGINAL_IMAGE,
                )
        dimensions = re.search(r"(?:image|original)[_ ]?(?:size)?\s*[=:]\s*(\d+)\s*[xX,]\s*(\d+)", block, re.I)
        if not (payload or trigger or roi or dimensions):
            return None
        return SurfaceDiagnostic(signal=self._int(payload.get("detSignal")), total=self._int(payload.get("detTotal")),
                                 trigger_box=trigger, roi_box=roi,
                                 image_width=int(dimensions.group(1)) if dimensions else None,
                                 image_height=int(dimensions.group(2)) if dimensions else None)

    @staticmethod
    def _box(value: Any, space: CoordinateSpace) -> BoxXYXY | None:
        if isinstance(value, list) and len(value) >= 4:
            return BoxXYXY(x1=float(value[0]), y1=float(value[1]), x2=float(value[2]), y2=float(value[3]), coordinate_space=space)
        return None

    def _parse_det(self, block: str, surface: SurfaceDiagnostic | None) -> list[DetBoxDiagnostic]:
        headers = list(re.finditer(r"\[DET-RAW\]\s*box\[(\d+)\]", block, re.I))
        result = []
        for pos, header in enumerate(headers):
            end = headers[pos + 1].start() if pos + 1 < len(headers) else min(len(block), header.start() + 1500)
            section = block[header.start():end]
            point_match = re.search(r"points\s*=\s*((?:\(\s*%s\s*,\s*%s\s*\)\s*,?\s*)+)" % (self.NUMBER, self.NUMBER), section, re.I)
            if not point_match:
                continue
            pairs = re.findall(r"\(\s*(%s)\s*,\s*(%s)\s*\)" % (self.NUMBER, self.NUMBER), point_match.group(1))
            points = [Point(x=float(x), y=float(y)) for x, y in pairs]
            raw = Polygon(points=points, coordinate_space=CoordinateSpace.SURFACE_ROI)
            original = None
            if surface and surface.roi_box:
                original = Polygon(points=[Point(x=p.x + surface.roi_box.x1, y=p.y + surface.roi_box.y1) for p in points], coordinate_space=CoordinateSpace.ORIGINAL_IMAGE)
            index = int(header.group(1))
            filtered = bool(re.search(rf"\[DET-FILTER\][\s\S]{{0,300}}box\[{index}\][\s\S]{{0,100}}(?:filtered|remove|drop)\s*[=:]\s*(?:1|true)", block, re.I))
            result.append(DetBoxDiagnostic(index=index, raw_polygon=raw, original_polygon=original, filtered=filtered))
        return result

    def _parse_deskew(self, block: str) -> DeskewDiagnostic | None:
        boxes = []
        for match in re.finditer(r"\[deskew\]\s*box\[(\d+)\](.*?)(?=\[deskew\]|\[CLS-|$)", block, re.I | re.S):
            values = self._pairs(match.group(2))
            boxes.append(DeskewBoxDiagnostic(box_index=int(match.group(1)), angle=self._float(values.get("angle")),
                                             aspect=self._float(values.get("aspect")), long_side=self._float(values.get("long_side")),
                                             weight=self._float(values.get("weight"))))
        final = re.search(r"\[deskew\]\s*final\s+angle\s*=\s*(%s)(.*?)(?=\[CLS-|$)" % self.NUMBER, block, re.I | re.S)
        if not boxes and not final:
            return None
        values = self._pairs(final.group(2)) if final else {}
        return DeskewDiagnostic(boxes=boxes, final_angle=float(final.group(1)) if final else None,
                                raw_estimate=self._float(values.get("raw_estimate")), consistency=self._float(values.get("consistency")),
                                conflict=self._bool(values.get("conflict")), method=values.get("method"))

    def _parse_cls(self, block: str):
        boxes = []
        for match in re.finditer(r"\[CLS-DUAL\]\s*box\[(\d+)\](.*?)(?=\[CLS-|\[deskew\]|$)", block, re.I | re.S):
            v = self._pairs(match.group(2)); crop = re.search(r"crop\s*=\s*(\d+)\s*[xX]\s*(\d+)", match.group(2))
            individual_rotate180 = self._bool(v.get("individual_rotate180"))
            is_reverse = self._bool(v.get("is_reverse"))
            if is_reverse is None:
                is_reverse = individual_rotate180
            boxes.append(CLSBoxDiagnostic(box_index=int(match.group(1)), original_p0=self._float(v.get("original_p0")),
                original_p180=self._float(v.get("original_p180")), rotated_p0=self._float(v.get("rotated_p0")),
                rotated_p180=self._float(v.get("rotated_p180")), dual_margin=self._float(v.get("dual_margin")),
                evidence=self._float(v.get("evidence")), individual_rotate180=individual_rotate180,
                is_reverse=is_reverse,
                crop_width=int(crop.group(1)) if crop else None, crop_height=int(crop.group(2)) if crop else None))
        global_match = re.search(r"\[CLS-GLOBAL\](.*?)(?=\[[A-Z-]+\]|推理结果|$)", block, re.I | re.S)
        global_result = None
        if global_match:
            v = self._pairs(global_match.group(1))
            global_result = CLSGlobalDiagnostic(**{key: converter(v.get(key)) for key, converter in {
                "evidence_sum": self._float, "evidence_abs_sum": self._float, "consensus": self._int,
                "positive": self._int, "negative": self._int, "zero": self._int, "conflict": self._bool,
                "anchor_box": self._int, "anchor_evidence": self._float, "method": lambda x: x,
                "uncertain": self._bool, "overrides": self._int, "boxes": self._int, "rotate180": self._bool,
            }.items()})
        return boxes, global_result

    def _parse_ocr(self, block: str, warnings: list[str]) -> OCRDiagnostic | None:
        payloads = self._json_objects_after(block, "推理结果:") or self._json_objects_after(block, "推理结果：")
        if not payloads:
            if "推理结果" in block:
                warnings.append("OCR 结果 JSON 不完整或无法解析")
            return None
        payload = payloads[-1]
        ocr = payload.get("ocrData") if isinstance(payload.get("ocrData"), dict) else payload
        raw_strings = ocr.get("stringList") or []
        strings = []
        for index, item in enumerate(raw_strings):
            if not isinstance(item, dict):
                item = {"stringName": str(item)}
            coords = item.get("stringCoordinate")
            polygon = None
            if isinstance(coords, list):
                if coords and isinstance(coords[0], (int, float)):
                    coords = list(zip(coords[::2], coords[1::2]))
                try:
                    polygon = Polygon(points=[Point(x=float(p[0]), y=float(p[1])) for p in coords], coordinate_space=CoordinateSpace.ORIGINAL_IMAGE)
                except (TypeError, ValueError, IndexError):
                    polygon = None
            text = str(item.get("stringName", ""))
            rec_method = item.get("recMethod")
            if rec_method is None:
                rec_method = item.get("rec_method")
            strings.append(OCRStringDiagnostic(
                index=index,
                text=text,
                length=self._int(item.get("length") or item.get("stringLength")),
                rec_method=self._int(rec_method),
                polygon=polygon,
            ))
        joined = "".join(item.text for item in strings)
        return OCRDiagnostic(success=self._bool(payload.get("success", ocr.get("success"))),
                             message=payload.get("message", ocr.get("message")), strings=strings,
                             full_picture_url=ocr.get("fullPictureUrl") or payload.get("fullPictureUrl"), joined_text=joined or None)

    def _pairs(self, text: str) -> dict[str, str]:
        return {m.group(1).lower(): m.group(2).strip().strip('"') for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,\s\]\r\n]+)", text)}

    @staticmethod
    def _float(value):
        try: return float(value) if value is not None else None
        except (TypeError, ValueError): return None

    @staticmethod
    def _int(value):
        try: return int(float(value)) if value is not None else None
        except (TypeError, ValueError): return None

    @staticmethod
    def _bool(value):
        if value is None: return None
        if isinstance(value, bool): return value
        return str(value).strip().lower() in {"1", "true", "yes"}

