from app.core.registry import build_sink, build_source
from app.diagnostics.registry import build_parser, build_renderer, build_resolver
from app.projects.binxin_72_79.diagnostics.parser import Binxin7279LogParser
from app.projects.binxin_72_79.diagnostics.renderer import Binxin7279Renderer
from app.projects.binxin_72_79.diagnostics.resolver import Binxin7279Resolver
from app.projects.binxin_72_79.sink import Binxin7279QingTuiSink
from app.projects.binxin_72_79.source import Binxin7279HistorySource
from app.projects.binxin_73_84.diagnostics.parser import Binxin7384LogParser
from app.projects.binxin_73_84.diagnostics.renderer import Binxin7384Renderer
from app.projects.binxin_73_84.diagnostics.resolver import Binxin7384Resolver
from app.projects.binxin_73_84.sink import Binxin7384QingTuiSink
from app.projects.binxin_73_84.source import Binxin7384HistorySource
from app.projects.binxin_74_71.diagnostics.parser import Binxin7471LogParser
from app.projects.binxin_74_71.diagnostics.renderer import Binxin7471Renderer
from app.projects.binxin_74_71.diagnostics.resolver import Binxin7471Resolver
from app.projects.binxin_74_71.sink import Binxin7471QingTuiSink
from app.projects.binxin_74_71.source import Binxin7471HistorySource
from app.projects.binxin_76.diagnostics.parser import Binxin76LogParser
from app.projects.binxin_76.diagnostics.renderer import Binxin76Renderer
from app.projects.binxin_76.diagnostics.resolver import Binxin76Resolver
from app.projects.binxin_76.sink import Binxin76QingTuiSink
from app.projects.binxin_76.source import Binxin76HistorySource
from app.projects.binxin_77.diagnostics.parser import Binxin77LogParser
from app.projects.binxin_77.diagnostics.renderer import Binxin77Renderer
from app.projects.binxin_77.diagnostics.resolver import Binxin77Resolver
from app.projects.binxin_77.sink import Binxin77QingTuiSink
from app.projects.binxin_77.source import Binxin77HistorySource
from app.projects.ruifeng.source import RuifengHistorySource


def _project(source_type: str, sink_type: str) -> dict:
    return {
        "source": {"type": source_type},
        "sink": {"type": sink_type},
    }


def test_projects_resolve_to_distinct_source_and_sink_classes():
    p7384 = _project("binxin_73_84_history", "binxin_73_84_qingtui")
    p7279 = _project("binxin_72_79_history", "binxin_72_79_qingtui")
    p7471 = _project("binxin_74_71_history", "binxin_74_71_qingtui")
    p76 = _project("binxin_76_history", "binxin_76_qingtui")
    p77 = _project("binxin_77_history", "binxin_77_qingtui")
    assert isinstance(build_source(p7384), Binxin7384HistorySource)
    assert isinstance(build_source(p7279), Binxin7279HistorySource)
    assert isinstance(build_sink(p7384), Binxin7384QingTuiSink)
    assert isinstance(build_sink(p7279), Binxin7279QingTuiSink)
    assert isinstance(build_source(p7471), Binxin7471HistorySource)
    assert isinstance(build_sink(p7471), Binxin7471QingTuiSink)
    assert type(build_source(p76)) is Binxin76HistorySource
    assert type(build_sink(p76)) is Binxin76QingTuiSink
    assert type(build_source(p77)) is Binxin77HistorySource
    assert type(build_sink(p77)) is Binxin77QingTuiSink


def test_ruifeng_source_is_registered_without_reusing_binxin_business_logic():
    project = {
        "source": {
            "type": "ruifeng_history",
            "camera_groups": ["渣跨1号废钢台车"],
        },
        "sink": {"enabled": False},
    }
    source = build_source(project)
    assert isinstance(source, RuifengHistorySource)
    assert not isinstance(source, Binxin7384HistorySource)


def test_projects_resolve_to_distinct_diagnostic_components():
    assert isinstance(build_parser({"type": "binxin_73_84_ocr"}), Binxin7384LogParser)
    assert isinstance(build_parser({"type": "binxin_72_79_ocr"}), Binxin7279LogParser)
    assert isinstance(build_resolver({"type": "binxin_73_84"}), Binxin7384Resolver)
    assert isinstance(build_resolver({"type": "binxin_72_79"}), Binxin7279Resolver)
    assert isinstance(build_renderer({"type": "binxin_73_84"}), Binxin7384Renderer)
    assert isinstance(build_renderer({"type": "binxin_72_79"}), Binxin7279Renderer)
    assert isinstance(build_parser({"type": "binxin_74_71_ocr"}), Binxin7471LogParser)
    assert isinstance(build_resolver({"type": "binxin_74_71"}), Binxin7471Resolver)
    assert isinstance(build_renderer({"type": "binxin_74_71"}), Binxin7471Renderer)
    assert type(build_parser({"type": "binxin_76_ocr"})) is Binxin76LogParser
    assert type(build_resolver({"type": "binxin_76"})) is Binxin76Resolver
    assert type(build_renderer({"type": "binxin_76"})) is Binxin76Renderer
    assert type(build_parser({"type": "binxin_77_ocr"})) is Binxin77LogParser
    assert type(build_resolver({"type": "binxin_77"})) is Binxin77Resolver
    assert type(build_renderer({"type": "binxin_77"})) is Binxin77Renderer
