from datetime import datetime
import json

import pytest

from app.diagnostics.models import DiagnosticQuery
from app.projects.binxin_76.diagnostics.parser import Binxin76LogParser
from app.projects.binxin_77.diagnostics.parser import Binxin77LogParser


@pytest.mark.parametrize(
    ("parser_class", "project_id"),
    [
        (Binxin76LogParser, "binxin_76"),
        (Binxin77LogParser, "binxin_77"),
    ],
)
def test_edge_upload_result_log_is_parsed_and_matched(parser_class, project_id):
    event_time = datetime(2026, 9, 4, 11, 25, 22, 103000)
    payload = {
        "accTime": int(event_time.timestamp() * 1000),
        "success": True,
        "message": "ok",
        "data": {
            "fullPictureUrl": (
                "http://198.51.100.85:19000/ocr-identification-76/"
                "algo-active/2026-09-04/origin_1788492171597.jpg"
            ),
            "stringList": [
                {
                    "stringName": "210410",
                    "stringLength": 6,
                    "recMethod": 0,
                    "stringCoordinate": [
                        {"x": 10, "y": 20},
                        {"x": 30, "y": 20},
                        {"x": 30, "y": 40},
                        {"x": 10, "y": 40},
                    ],
                },
                {"stringName": "210410"},
                {"stringName": "E4"},
                {"stringName": "ET"},
            ],
        },
    }
    line = (
        "[2026-09-04 11:25:22.157][info][logicProcessTask.cpp:565]"
        f"ocr upload result:{json.dumps(payload, ensure_ascii=False)}"
    )
    parser = parser_class(
        {
            "match": {
                "max_time_difference_seconds": 20,
                "time_weight": 30,
                "result_weight": 50,
                "image_name_weight": 100,
            }
        }
    )

    events = parser.parse_events(f"[2026-09-04 11:25:21] unrelated\n{line}\n")

    assert len(events) == 1
    event = events[0]
    assert event.trigger_time == event_time
    assert event.image_name == "origin_1788492171597.jpg"
    assert event.details.ocr.success is True
    assert event.details.ocr.joined_text == "210410210410E4ET"
    assert event.details.ocr.strings[0].polygon.points[0].x == 10

    query = DiagnosticQuery(
        project_id=project_id,
        event_time=event_time,
        expected_result="210410ET",
        image_name="origin_1788492171597.jpg",
    )
    assert parser.score_event(event, query) == 180


@pytest.mark.parametrize("parser_class", [Binxin76LogParser, Binxin77LogParser])
def test_edge_parser_ignores_unrelated_live_log_lines(parser_class):
    parser = parser_class({})
    assert parser.parse_events("[2026-09-04 11:25:22.157][info]heartbeat") == []
