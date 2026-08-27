from datetime import datetime

from app.diagnostics.models import DiagnosticQuery
from app.diagnostics.models import DiagnosticResult
from app.diagnostics.service import LogDiagnosticService
from app.projects.binxin_73_84.diagnostics.parser import Binxin7384LogParser
from app.projects.binxin_73_84.diagnostics.renderer import Binxin7384Renderer
from app.projects.binxin_73_84.diagnostics.resolver import Binxin7384Resolver


SAMPLE = r"""
[2026-08-24 17:25:16.900] SteelDet,alarm=1,alarm_count=7,alarm_count_thr=7
[2026-08-24 17:25:17.000] ----------------------------取到图片开始一次推理---------------------------
[SURFACE-UPSTREAM] json={"detList":[{"detCoordinate":[903.191,639.192,1600.900,1367.876]}],"detSignal":1,"detTotal":1}
[SURFACE-UPSTREAM][PARSE] index=0 raw_xyxy=(903.191,639.192,1600.900,1367.876) roi=(833,566,700,800)
[SURFACE-ROI] source=upstream_json original=2456x1500 roi=(833,566,700,800) text_det_count=2
[DET-RAW] box_count=2
[DET-RAW] box[0] point_count=4 points=(233,130),(300,131),(301,200),(234,199)
[DET-RAW] box[1] point_count=4 points=(20,30),(80,30),(80,60),(20,60)
[deskew] box[0], angle=-88.6816, aspect=3.52702, long_side=591.309, weight=1
[deskew] final angle=-89.5781, raw_estimate=-89.5781, consistency=0.98, conflict=0, method=multi_box_consensus
[CLS-DUAL] box[0], original_p0=0.00879129, original_p180=0.991209, rotated_p0=0.0128474, rotated_p180=0.987153, dual_margin=0.00405612, evidence=-0.383481, individual_rotate180=1, crop=542x241
[CLS-GLOBAL] evidence_sum=-7.15597, evidence_abs_sum=7.15597, consensus=1, positive=0, negative=2, zero=0, conflict=0, anchor_box=1, anchor_evidence=-6.77249, method=unanimous_rotate, uncertain=0, rotate180=1
[2026-08-24 17:25:18.000] steel_number_check_algo_task.cpp:372]推理结果:{"message":"ok","success":true,"ocrData":{"fullPictureUrl":"http://box/a.jpg","stringList":[{"stringName":"10135","stringLength":5,"recMethod":0},{"stringName":"X5X","recMethod":2}]}}
[2026-08-24 17:25:18.100] ----------------------------本次取图推理结束------------------------------
"""


def parser():
    return Binxin7384LogParser({"match": {"max_time_difference_seconds": 20, "time_weight": 30, "result_weight": 50}})


def resolver():
    return Binxin7384Resolver({})


def test_structured_fields_and_coordinate_transform():
    event = parser().parse_events(SAMPLE)[0]
    data = event.details
    assert data.surface.signal == 1
    assert data.surface.total == 1
    assert data.surface.trigger_box.x1 == 903.191
    assert data.surface.roi_box.x1 == 833
    assert len(data.det_boxes) == 2
    assert data.det_boxes[0].raw_polygon.coordinate_space == "surface_roi"
    assert data.det_boxes[0].original_polygon.points[0].x == 1066
    assert data.det_boxes[0].original_polygon.points[0].y == 696
    assert data.deskew.final_angle == -89.5781
    assert data.deskew.boxes[0].aspect == 3.52702
    assert data.cls_boxes[0].original_p180 == 0.991209
    assert data.cls_boxes[0].is_reverse is True
    assert data.cls_global.rotate180 is True
    assert data.ocr.joined_text == "10135X5X"
    assert len(data.ocr.strings) == 2
    assert data.ocr.success is True
    assert data.ocr.message == "ok"
    assert data.ocr.strings[0].rec_method == 0
    assert data.ocr.strings[0].length == 5


def test_render_overlays_put_cls_probabilities_on_det_labels():
    query = DiagnosticQuery(
        project_id="binxin_billet",
        event_time=datetime(2026, 8, 24, 17, 25, 17),
    )
    event = parser().parse_events(SAMPLE)[0]
    payload = Binxin7384Renderer().build(
        DiagnosticResult(
            matched=True,
            parser_type="binxin_ocr",
            query=query,
            event=event,
        )
    )
    assert payload.overlays[0].label == "Surface Detect"
    assert "P0 0.0088" in payload.overlays[1].label
    assert "P180 0.9912" in payload.overlays[1].label
    assert "is_reverse=true" in payload.overlays[1].label
    assert payload.overlays[1].state == "reverse"
    assert payload.overlays[-1].role == "deskew"
    assert "顺时针" in payload.overlays[-1].label


def test_selection_uses_time_and_expected_result():
    second = SAMPLE.replace("17:25:17", "17:25:27").replace("10135", "OTHER")
    events = parser().parse_events(SAMPLE + "\n" + second)
    query = DiagnosticQuery(project_id="binxin_billet", event_time=datetime(2026, 8, 24, 17, 25, 17), expected_result="10135X5X")
    selected, score, alternatives = parser().select_event(events, query)
    assert selected.details.ocr.joined_text == "10135X5X"
    assert score >= 50
    assert alternatives


def test_missing_optional_fields_and_invalid_input_are_tolerated():
    partial = SAMPLE.replace(", consistency=0.98", "").replace(", uncertain=0", "")
    event = parser().parse_events(partial)[0]
    assert event.details.deskew.consistency is None
    assert event.details.cls_global.uncertain is None
    assert parser().parse_events("[2026-08-24 17:25:17] unrelated") == []


def test_untimestamped_protocol_lines_remain_inside_event():
    event = parser().parse_events(SAMPLE)[0]
    assert "[DET-RAW]" in event.raw_log
    assert "[CLS-DUAL]" in event.raw_log


def test_ocr_empty_fallback_is_opt_in_and_requires_a_matched_event():
    query = DiagnosticQuery(
        project_id="binxin_billet",
        event_time=datetime(2026, 8, 24, 17, 25, 17),
    )
    event = parser().parse_events(SAMPLE)[0]
    result = DiagnosticResult(
        matched=True, parser_type="binxin_ocr", query=query, event=event
    )
    fallback = {"when": "ocr_empty"}
    assert resolver().should_fallback(result, fallback) is False

    event.details.ocr.joined_text = None
    assert resolver().should_fallback(result, fallback) is True

    result.matched = False
    assert resolver().should_fallback(result, fallback) is False


def test_fallback_inherits_log_and_parser_but_overrides_source():
    primary = {
        "source": {"host": "172.30.31.73"},
        "log": {"path": "/same.log", "before_seconds": 8},
        "parser": {"type": "binxin_ocr", "match": {"time_weight": 30}},
    }
    fallback = {
        "source": {"host": "172.30.31.84"},
        "log": {"after_seconds": 20},
    }
    merged = LogDiagnosticService._fallback_configuration(primary, fallback)
    assert merged["source"]["host"] == "172.30.31.84"
    assert merged["log"] == {
        "path": "/same.log", "before_seconds": 8, "after_seconds": 20
    }
    assert merged["parser"] == primary["parser"]


def test_matched_but_empty_alternate_still_matches_fallback_condition():
    query = DiagnosticQuery(
        project_id="binxin_billet",
        event_time=datetime(2026, 8, 24, 17, 25, 17),
    )
    event = parser().parse_events(SAMPLE)[0]
    event.details.ocr.joined_text = ""
    alternate = DiagnosticResult(
        matched=True, parser_type="binxin_ocr", query=query, event=event
    )
    assert resolver().should_fallback(alternate, {"when": "ocr_empty"}) is True
