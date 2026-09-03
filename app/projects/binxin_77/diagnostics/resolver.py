from __future__ import annotations

from app.diagnostics.models import DiagnosticResult
from app.diagnostics.resolver import DiagnosticResolver
from app.projects.binxin_77.diagnostics.models import BinxinDiagnosticData


class Binxin77Resolver(DiagnosticResolver):
    """Reusable policy template for paired Binxin OCR machines."""

    resolver_type = "binxin_77"

    def should_fallback(self, result: DiagnosticResult, fallback: dict) -> bool:
        condition = str(fallback.get("when", "")).strip()
        if condition != "ocr_empty":
            raise ValueError(f"不支持的镔鑫 fallback 条件: {condition}")
        if not result.matched or not result.event:
            return False
        details = result.event.details
        if not isinstance(details, BinxinDiagnosticData):
            return False
        text = details.ocr.joined_text if details.ocr else None
        return not (text or "").strip()




