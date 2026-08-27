from datetime import datetime, timedelta

import pytest

from app.diagnostics.models import DiagnosticEvent, DiagnosticSearchQuery
from app.diagnostics.service import LogDiagnosticService


def _query(**overrides) -> DiagnosticSearchQuery:
    values = {
        "project_id": "binxin_billet_74",
        "start_time": datetime(2026, 8, 26, 10, 0, 0),
        "end_time": datetime(2026, 8, 26, 10, 10, 0),
        "expected_result": "3MSC9968",
        "image_name": "identifyPicture_123.jpg",
    }
    values.update(overrides)
    return DiagnosticSearchQuery(**values)


def test_manual_search_matches_time_result_and_image_name():
    event = DiagnosticEvent(
        trigger_time=datetime(2026, 8, 26, 10, 5, 0),
        image_name="1_identifyPicture_123.jpg",
        raw_log='推理结果: {"stringName":"3MSC9968"}',
    )
    assert LogDiagnosticService()._event_matches(event, _query())


def test_manual_search_rejects_event_outside_requested_range():
    event = DiagnosticEvent(
        trigger_time=datetime(2026, 8, 26, 10, 15, 0),
        image_name="identifyPicture_123.jpg",
        raw_log="3MSC9968",
    )
    assert not LogDiagnosticService()._event_matches(event, _query())


def test_manual_search_limits_window_to_two_hours():
    query = _query(end_time=datetime(2026, 8, 26, 12, 0, 1))
    with pytest.raises(ValueError, match="不能超过 2 小时"):
        LogDiagnosticService().search_for_render(query)

