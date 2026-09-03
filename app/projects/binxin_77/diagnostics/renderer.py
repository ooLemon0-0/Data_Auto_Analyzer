from app.diagnostics.models import DiagnosticRenderPayload, DiagnosticResult, RenderOverlay
from app.diagnostics.renderer import DiagnosticRenderer
from app.projects.binxin_77.diagnostics.models import BinxinDiagnosticData


class Binxin77Renderer(DiagnosticRenderer):
    """Reusable rendering template for Binxin diagnostic details."""

    renderer_type = "binxin_77"

    def build(self, result: DiagnosticResult) -> DiagnosticRenderPayload:
        overlays: list[RenderOverlay] = []
        event = result.event
        details = event.details if event and isinstance(event.details, BinxinDiagnosticData) else None
        if details and details.surface and details.surface.trigger_box:
            overlays.append(RenderOverlay(
                role="surface_trigger", label="Surface Detect",
                box=details.surface.trigger_box,
            ))
        if details:
            cls_by_index = {item.box_index: item for item in details.cls_boxes}
            for det in details.det_boxes:
                if det.original_polygon:
                    cls = cls_by_index.get(det.index)
                    label = f"DET {det.index}"
                    if cls:
                        p0 = "—" if cls.original_p0 is None else f"{cls.original_p0:.4f}"
                        p180 = "—" if cls.original_p180 is None else f"{cls.original_p180:.4f}"
                        if cls.is_reverse is None:
                            direction, state = "方向未知", "unknown"
                        elif cls.is_reverse:
                            direction, state = "反向", "reverse"
                        else:
                            direction, state = "正向", "upright"
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
            angle = details.deskew.final_angle if details.deskew else None
            if angle is None:
                deskew_label, deskew_state = "Deskew：方向未知", "unknown"
            elif abs(angle) < 0.5:
                deskew_label, deskew_state = f"Deskew：未旋转 {angle:+.2f}°", "none"
            elif angle > 0:
                deskew_label, deskew_state = f"Deskew：↺ 逆时针 {angle:+.2f}°", "counterclockwise"
            else:
                deskew_label, deskew_state = f"Deskew：↻ 顺时针 {angle:+.2f}°", "clockwise"
            overlays.append(RenderOverlay(
                role="deskew", label=deskew_label, state=deskew_state,
            ))
        surface = details.surface if details else None
        return DiagnosticRenderPayload(
            result=result,
            image_width=surface.image_width if surface else None,
            image_height=surface.image_height if surface else None,
            overlays=overlays,
        )





