"""Project plugin registration only; business behaviour stays in project packages."""


def register_data_plugins() -> None:
    from app.core.registry import register_sink, register_source
    from app.projects.binxin_76.sink import Binxin76QingTuiSink
    from app.projects.binxin_76.source import Binxin76HistorySource
    from app.projects.binxin_77.sink import Binxin77QingTuiSink
    from app.projects.binxin_77.source import Binxin77HistorySource
    from app.projects.binxin_72_79.sink import Binxin7279QingTuiSink
    from app.projects.binxin_72_79.source import Binxin7279HistorySource
    from app.projects.binxin_73_84.sink import Binxin7384QingTuiSink
    from app.projects.binxin_73_84.source import Binxin7384HistorySource
    from app.projects.binxin_74_71.sink import Binxin7471QingTuiSink
    from app.projects.binxin_74_71.source import Binxin7471HistorySource
    from app.projects.ruifeng.source import RuifengHistorySource

    register_source("binxin_73_84_history", Binxin7384HistorySource)
    register_sink("binxin_73_84_qingtui", Binxin7384QingTuiSink)
    register_source("binxin_72_79_history", Binxin7279HistorySource)
    register_sink("binxin_72_79_qingtui", Binxin7279QingTuiSink)
    register_source("binxin_74_71_history", Binxin7471HistorySource)
    register_sink("binxin_74_71_qingtui", Binxin7471QingTuiSink)
    register_source("binxin_76_history", Binxin76HistorySource)
    register_sink("binxin_76_qingtui", Binxin76QingTuiSink)
    register_source("binxin_77_history", Binxin77HistorySource)
    register_sink("binxin_77_qingtui", Binxin77QingTuiSink)
    register_source("ruifeng_history", RuifengHistorySource)


def register_diagnostic_plugins() -> None:
    from app.diagnostics.registry import (
        register_parser,
        register_renderer,
        register_resolver,
    )
    from app.projects.binxin_72_79.diagnostics.parser import Binxin7279LogParser
    from app.projects.binxin_72_79.diagnostics.renderer import Binxin7279Renderer
    from app.projects.binxin_72_79.diagnostics.resolver import Binxin7279Resolver
    from app.projects.binxin_73_84.diagnostics.parser import Binxin7384LogParser
    from app.projects.binxin_73_84.diagnostics.renderer import Binxin7384Renderer
    from app.projects.binxin_73_84.diagnostics.resolver import Binxin7384Resolver
    from app.projects.binxin_74_71.diagnostics.parser import Binxin7471LogParser
    from app.projects.binxin_74_71.diagnostics.renderer import Binxin7471Renderer
    from app.projects.binxin_74_71.diagnostics.resolver import Binxin7471Resolver
    from app.projects.binxin_76.diagnostics.parser import Binxin76LogParser
    from app.projects.binxin_76.diagnostics.renderer import Binxin76Renderer
    from app.projects.binxin_76.diagnostics.resolver import Binxin76Resolver
    from app.projects.binxin_77.diagnostics.parser import Binxin77LogParser
    from app.projects.binxin_77.diagnostics.renderer import Binxin77Renderer
    from app.projects.binxin_77.diagnostics.resolver import Binxin77Resolver

    register_parser(Binxin7384LogParser)
    register_resolver(Binxin7384Resolver)
    register_renderer(Binxin7384Renderer)
    register_parser(Binxin7279LogParser)
    register_resolver(Binxin7279Resolver)
    register_renderer(Binxin7279Renderer)
    register_parser(Binxin7471LogParser)
    register_resolver(Binxin7471Resolver)
    register_renderer(Binxin7471Renderer)
    register_parser(Binxin76LogParser)
    register_resolver(Binxin76Resolver)
    register_renderer(Binxin76Renderer)
    register_parser(Binxin77LogParser)
    register_resolver(Binxin77Resolver)
    register_renderer(Binxin77Renderer)
