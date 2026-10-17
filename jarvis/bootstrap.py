"""The composition root: hands the lower layers what sits above them and only meets them at run time. Every process
entry point calls wire() before it starts work (jarvis.apps.*, python -m jarvis.node, the tests' conftest).

  task sessions   JARVIS's own tools as an MCP server, and session options with tool results screened (brain.sdk)
  watches         the tool registry, to call a reading tool (jarvis.tools)
"""

from types import SimpleNamespace

_wired = False


def wire() -> None:
    """Wire the parts once per process; calling again does nothing."""
    global _wired
    if _wired:
        return
    from jarvis import tools
    from jarvis.brain import sdk
    from jarvis.tasks import tool_watch, worker
    from jarvis.tools import google

    # Looked up at each call, so a module's function replaced later (a test's stand-in) is the one used
    worker.use(worker.SessionTools(server=lambda *a, **k: sdk.jarvis_server(*a, **k),
                                   options=lambda *a, **k: sdk.sdk_options(*a, **k)))
    tool_watch.use(SimpleNamespace(get=lambda name: tools.get(name), input_schema=lambda t: tools.input_schema(t),
                                   run=lambda name, args: tools.run(name, args), fresh=lambda: google.fresh()))
    _wired = True
