from __future__ import annotations

from collections.abc import Callable

from app.sinks.base import DataSink
from app.sources.base import DataSource

SOURCE_FACTORIES: dict[str, Callable[[dict], DataSource]] = {}
SINK_FACTORIES: dict[str, Callable[[dict], DataSink]] = {}


def register_source(source_type: str, factory: Callable[[dict], DataSource]) -> None:
    SOURCE_FACTORIES[source_type] = factory


def register_sink(sink_type: str, factory: Callable[[dict], DataSink]) -> None:
    SINK_FACTORIES[sink_type] = factory


def build_source(project: dict):
    source_type = project["source"].get("type")
    if source_type not in SOURCE_FACTORIES:
        raise ValueError(f"Unsupported source type: {source_type}")
    return SOURCE_FACTORIES[source_type](project)


def build_sink(project: dict):
    sink_type = project.get("sink", {}).get("type")
    if sink_type not in SINK_FACTORIES:
        raise ValueError(f"Unsupported sink type: {sink_type}")
    return SINK_FACTORIES[sink_type](project)


from app.projects.plugins import register_data_plugins

register_data_plugins()
