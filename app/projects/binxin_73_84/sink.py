from __future__ import annotations

from datetime import date
from typing import Any

from app.sinks.qingtui_document import QingTuiDocumentSink


class Binxin7384QingTuiSink(QingTuiDocumentSink):
    """73/84 upload entry; routing remains owned by this project's config."""

    def prepare_auth(self) -> dict[str, Any]:
        return super().prepare_auth()

    def upload_day(self, business_date: date, summary: dict[str, Any]) -> dict[str, Any]:
        return super().upload_day(business_date, summary)
