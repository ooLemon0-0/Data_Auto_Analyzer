from __future__ import annotations

from collections.abc import Callable

from app.diagnostics.sources.base import LogSource

PARSERS: dict[str, type] = {}
SOURCES: dict[str, Callable[[dict], LogSource]] = {}


def register_parser(parser_cls):
    PARSERS[parser_cls.parser_type] = parser_cls
    return parser_cls


def register_source(source_type: str, factory: Callable[[dict], LogSource]) -> None:
    SOURCES[source_type] = factory


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


def _register_builtins() -> None:
    from app.diagnostics.sources.ssh_file import SSHFileLogSource
    from app.projects.binxin.diagnostics.parser import BinxinLogParser
    register_source("ssh_file", SSHFileLogSource)
    register_parser(BinxinLogParser)


_register_builtins()
