from __future__ import annotations

from collections.abc import Callable

from app.diagnostics.sources.base import LogSource
from app.diagnostics.renderer import DiagnosticRenderer
from app.diagnostics.resolver import DiagnosticResolver

PARSERS: dict[str, type] = {}
SOURCES: dict[str, Callable[[dict], LogSource]] = {}
RESOLVERS: dict[str, type[DiagnosticResolver]] = {}
RENDERERS: dict[str, type[DiagnosticRenderer]] = {}


def register_parser(parser_cls):
    PARSERS[parser_cls.parser_type] = parser_cls
    return parser_cls


def register_source(source_type: str, factory: Callable[[dict], LogSource]) -> None:
    SOURCES[source_type] = factory


def register_resolver(resolver_cls):
    RESOLVERS[resolver_cls.resolver_type] = resolver_cls
    return resolver_cls


def register_renderer(renderer_cls):
    RENDERERS[renderer_cls.renderer_type] = renderer_cls
    return renderer_cls


def build_parser(config: dict):
    parser_type = config.get("type", "")
    if parser_type not in PARSERS:
        raise ValueError(f"未注册日志 parser: {parser_type}")
    return PARSERS[parser_type](config)


def build_log_source(config: dict, log_config: dict) -> LogSource:
    source_type = config.get("type", "")
    if source_type not in SOURCES:
        raise ValueError(f"未注册日志 source: {source_type}")
    return SOURCES[source_type]({**config, "log": log_config})


def build_resolver(config: dict) -> DiagnosticResolver:
    resolver_type = config.get("type", "")
    if resolver_type not in RESOLVERS:
        raise ValueError(f"未注册诊断 resolver: {resolver_type}")
    return RESOLVERS[resolver_type](config)


def build_renderer(config: dict) -> DiagnosticRenderer:
    renderer_type = config.get("type", "")
    if renderer_type not in RENDERERS:
        raise ValueError(f"未注册诊断 renderer: {renderer_type}")
    return RENDERERS[renderer_type](config)


def _register_builtins() -> None:
    from app.diagnostics.sources.ssh_file import SSHFileLogSource
    register_source("ssh_file", SSHFileLogSource)
    from app.projects.plugins import register_diagnostic_plugins
    register_diagnostic_plugins()


_register_builtins()
