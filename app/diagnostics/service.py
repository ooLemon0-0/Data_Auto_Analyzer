from __future__ import annotations

from datetime import timedelta
import logging

from app.core.config import settings
from app.diagnostics.models import DiagnosticQuery, DiagnosticResult
from app.diagnostics.registry import build_log_source, build_parser
from app.diagnostics.render.builder import RenderPayloadBuilder
from app.diagnostics.sources.ssh_file import LogSourceError

logger = logging.getLogger("data_review_platform.diagnostics")


class LogDiagnosticService:
    def _configuration(self, query: DiagnosticQuery) -> dict:
        project = settings.project(query.project_id)
        root = project.diagnostics
        if not root or not root.get("enabled", False):
            raise ValueError("当前项目未配置日志诊断功能")
        stations = root.get("stations") or {}
        if stations:
            key = str(query.station or root.get("default_station", ""))
            if key not in stations:
                raise ValueError("当前机位没有配置日志源")
            station = stations[key]
            return {**root, **station, "parser": station.get("parser", root.get("parser", {}))}
        return root

    def analyze(self, query: DiagnosticQuery) -> DiagnosticResult:
        cfg = self._configuration(query)
        parser = build_parser(cfg["parser"])
        log_cfg = cfg["log"]
        start = query.event_time - timedelta(seconds=float(log_cfg.get("before_seconds", 8)))
        end = query.event_time + timedelta(seconds=float(log_cfg.get("after_seconds", 15)))
        try:
            chunk = build_log_source(cfg["source"], log_cfg).fetch(start, end)
        except LogSourceError as exc:
            logger.warning(
                "Log diagnostics source failed: project=%s station=%s error=%s",
                query.project_id,
                query.station or cfg.get("default_station", ""),
                exc,
            )
            return DiagnosticResult(matched=False, parser_type=parser.parser_type, query=query, warnings=[str(exc)])
        if not chunk.raw_text.strip():
            return DiagnosticResult(matched=False, parser_type=parser.parser_type, query=query, warnings=["时间范围内没有日志"])
        events = parser.parse_events(chunk.raw_text)
        best, score, alternatives = parser.select_event(events, query)
        max_delta = float(cfg["parser"].get("match", {}).get("max_time_difference_seconds", 20))
        if best and (best.trigger_time or best.start_time):
            delta = abs(((best.trigger_time or best.start_time) - query.event_time).total_seconds())
            if delta > max_delta:
                best = None
        warnings = []
        if len(events) > 1:
            warnings.append(f"找到 {len(events)} 个候选事件，已返回最高匹配项")
        if best:
            warnings.extend(best.warnings)
        else:
            warnings.append("未找到匹配的 OCR 推理事件")
        return DiagnosticResult(matched=best is not None, match_score=score if best else 0,
                                parser_type=parser.parser_type, query=query, event=best,
                                warnings=warnings, alternatives=alternatives)

    def analyze_for_render(self, query: DiagnosticQuery):
        return RenderPayloadBuilder().build(self.analyze(query))


diagnostic_service = LogDiagnosticService()
