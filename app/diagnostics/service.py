from __future__ import annotations

from datetime import timedelta
import logging

from app.core.config import settings
from app.diagnostics.models import (
    DiagnosticQuery,
    DiagnosticResult,
    DiagnosticSearchMatch,
    DiagnosticSearchQuery,
    DiagnosticSearchResult,
)
from app.diagnostics.registry import (
    build_log_source,
    build_parser,
    build_renderer,
    build_resolver,
)
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

    def _analyze_once(self, query: DiagnosticQuery, cfg: dict) -> DiagnosticResult:
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

    @staticmethod
    def _fallback_configuration(primary: dict, fallback: dict) -> dict:
        """Fallback entries inherit parser/log defaults but must name their own source."""
        if not fallback.get("source"):
            raise ValueError("诊断 fallback 缺少 source 配置")
        return {
            **primary,
            **fallback,
            "source": fallback["source"],
            "log": {**primary.get("log", {}), **fallback.get("log", {})},
            "parser": {**primary.get("parser", {}), **fallback.get("parser", {})},
        }

    def _analyze_configured(self, query: DiagnosticQuery, cfg: dict) -> DiagnosticResult:
        resolver = build_resolver(cfg["resolver"])
        primary = self._analyze_once(query, cfg)
        for fallback in cfg.get("fallbacks") or []:
            if not fallback.get("enabled", True):
                continue
            if not resolver.should_fallback(primary, fallback):
                continue
            try:
                alternate = self._analyze_once(
                    query, self._fallback_configuration(cfg, fallback)
                )
            except (KeyError, TypeError, ValueError) as exc:
                primary.warnings.append(f"备用日志源配置无效：{exc}")
                continue
            label = str(fallback.get("name") or fallback["source"].get("host") or "备用日志源")
            if alternate.matched and not resolver.should_fallback(alternate, fallback):
                alternate.warnings.insert(0, f"已按项目诊断策略改用 {label} 的事件和原图")
                return alternate
            if alternate.matched:
                detail = "匹配事件仍满足项目 fallback 条件"
            else:
                detail = "；".join(alternate.warnings) or "未找到匹配事件"
            primary.warnings.append(f"已查询 {label}，但未能取得备用事件：{detail}")
        return primary

    def analyze(self, query: DiagnosticQuery) -> DiagnosticResult:
        cfg = self._configuration(query)
        return self._analyze_configured(query, cfg)

    def analyze_for_render(self, query: DiagnosticQuery):
        cfg = self._configuration(query)
        result = self._analyze_configured(query, cfg)
        return build_renderer(cfg["renderer"]).build(result)

    @staticmethod
    def _event_time(event):
        return event.trigger_time or event.start_time or event.finish_time

    @staticmethod
    def _contains(value: str | None, needle: str) -> bool:
        return needle.casefold() in str(value or "").casefold()

    def _event_matches(self, event, query: DiagnosticSearchQuery) -> bool:
        event_time = self._event_time(event)
        if event_time is None or not (query.start_time <= event_time <= query.end_time):
            return False
        result_text = (query.expected_result or "").strip()
        image_name = (query.image_name or "").strip()
        if result_text and not self._contains(event.raw_log, result_text):
            return False
        if image_name and not any(
            self._contains(value, image_name)
            for value in (event.image_name, event.image_url, event.raw_log)
        ):
            return False
        return True

    def _search_source(
        self,
        query: DiagnosticSearchQuery,
        cfg: dict,
        source_name: str,
    ) -> tuple[list[DiagnosticSearchMatch], list[str]]:
        parser = build_parser(cfg["parser"])
        try:
            chunk = build_log_source(cfg["source"], cfg["log"]).fetch(
                query.start_time, query.end_time
            )
        except LogSourceError as exc:
            return [], [f"{source_name}：{exc}"]

        renderer = build_renderer(cfg["renderer"])
        matches: list[DiagnosticSearchMatch] = []
        for event in parser.parse_events(chunk.raw_text):
            if not self._event_matches(event, query):
                continue
            event_time = self._event_time(event)
            event_query = DiagnosticQuery(
                project_id=query.project_id,
                event_time=event_time,
                expected_result=(query.expected_result or "").strip() or None,
                station=query.station,
                image_name=(query.image_name or "").strip() or None,
            )
            result = DiagnosticResult(
                matched=True,
                match_score=parser.score_event(event, event_query),
                parser_type=parser.parser_type,
                query=event_query,
                event=event,
                warnings=list(event.warnings),
            )
            matches.append(
                DiagnosticSearchMatch(
                    source_name=source_name,
                    payload=renderer.build(result),
                )
            )
        return matches, []

    def search_for_render(self, query: DiagnosticSearchQuery) -> DiagnosticSearchResult:
        if query.end_time <= query.start_time:
            raise ValueError("结束时间必须晚于开始时间")
        if query.end_time - query.start_time > timedelta(hours=2):
            raise ValueError("单次日志查询时间段不能超过 2 小时")

        cfg = self._configuration(query)
        station = str(query.station or cfg.get("default_station") or "")
        host = str(cfg.get("source", {}).get("host") or "主日志源")
        sources = [(f"{station} 主服务器（{host}）", cfg)]
        for fallback in cfg.get("fallbacks") or []:
            if fallback.get("enabled", True):
                sources.append(
                    (
                        str(fallback.get("name") or fallback.get("source", {}).get("host") or "备用日志源"),
                        self._fallback_configuration(cfg, fallback),
                    )
                )

        matches: list[DiagnosticSearchMatch] = []
        warnings: list[str] = []
        for source_name, source_cfg in sources:
            found, source_warnings = self._search_source(query, source_cfg, source_name)
            matches.extend(found)
            warnings.extend(source_warnings)

        matches.sort(
            key=lambda item: self._event_time(item.payload.result.event) or query.start_time,
            reverse=True,
        )
        limit = 50
        if len(matches) > limit:
            warnings.append(f"匹配结果超过 {limit} 条，仅返回时间最新的 {limit} 条")
            matches = matches[:limit]
        if not matches and not warnings:
            warnings.append("指定时间段内没有找到符合条件的 OCR 推理事件")
        return DiagnosticSearchResult(matches=matches, warnings=warnings)


diagnostic_service = LogDiagnosticService()
