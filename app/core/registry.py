from __future__ import annotations

from app.projects.binxin.sink import QingTuiDocumentSink
from app.projects.binxin.source import BinxinHistorySource


def build_source(project: dict):
    source_type = project["source"].get("type")
    if source_type == "binxin_history":
        return BinxinHistorySource(project)
    raise ValueError(f"Unsupported source type: {source_type}")


def build_sink(project: dict):
    sink_type = project.get("sink", {}).get("type")
    if sink_type == "qingtui_document":
        return QingTuiDocumentSink(project)
    raise ValueError(f"Unsupported sink type: {sink_type}")
