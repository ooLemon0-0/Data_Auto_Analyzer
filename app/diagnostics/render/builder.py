from app.diagnostics.models import DiagnosticRenderPayload, DiagnosticResult, RenderOverlay


class RenderPayloadBuilder:
    def build(self, result: DiagnosticResult) -> DiagnosticRenderPayload:
        overlays: list[RenderOverlay] = []
        event = result.event
        if event and event.surface and event.surface.trigger_box:
            overlays.append(RenderOverlay(role="surface_trigger", label="Surface Detect", box=event.surface.trigger_box))
        if event:
            cls_by_index = {item.box_index: item for item in event.cls_boxes}
            for det in event.det_boxes:
                if det.original_polygon:
                    cls = cls_by_index.get(det.index)
                    label = f"DET {det.index}"
                    if cls:
                        p0 = "—" if cls.original_p0 is None else f"{cls.original_p0:.4f}"
                        p180 = "—" if cls.original_p180 is None else f"{cls.original_p180:.4f}"
                        if cls.is_reverse is None:
                            direction = "方向未知"
                            state = "unknown"
                        elif cls.is_reverse:
                            direction = "反向"
                            state = "reverse"
                        else:
                            direction = "正向"
                            state = "upright"
                        reverse_value = "—" if cls.is_reverse is None else str(cls.is_reverse).lower()
                        label += (
                            f"  P0 {p0}  P180 {p180}  {direction}  "
                            f"is_reverse={reverse_value}"
                        )
                    else:
                        state = "unknown"
                    overlays.append(RenderOverlay(
                        role="det", label=label, state=state,
                        polygon=det.original_polygon,
                    ))
            angle = event.deskew.final_angle if event.deskew else None
            if angle is None:
                deskew_label = "Deskew：方向未知"
                deskew_state = "unknown"
            elif abs(angle) < 0.5:
                deskew_label = f"Deskew：未旋转 {angle:+.2f}°"
                deskew_state = "none"
            elif angle > 0:
                deskew_label = f"Deskew：↺ 逆时针 {angle:+.2f}°"
                deskew_state = "counterclockwise"
            else:
                deskew_label = f"Deskew：↻ 顺时针 {angle:+.2f}°"
                deskew_state = "clockwise"
            overlays.append(RenderOverlay(
                role="deskew", label=deskew_label, state=deskew_state,
            ))
        return DiagnosticRenderPayload(result=result, overlays=overlays)
