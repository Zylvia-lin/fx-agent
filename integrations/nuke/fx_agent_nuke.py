"""Load the pinned bridge in Nuke 15.2. This module has no external dependencies."""

import sys
from pathlib import Path


def _dispatch_with_result(self, callback, *args):
    import fx_agent_nuke_addon

    nuke = fx_agent_nuke_addon._get_nuke()
    if not nuke.GUI:
        return self._fx_original_dispatch(callback, *args)

    def invoke():
        try:
            return {"ok": True, "value": callback(*args)}
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    result = nuke.executeInMainThreadWithResult(invoke, args=())
    if not isinstance(result, dict) or "ok" not in result:
        raise RuntimeError("Nuke main-thread dispatch returned no result")
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["value"]


def start(port=8187):
    if port == 8186:
        raise ValueError("Nuke must not use the FX Houdini default port 8186")
    vendor = str(Path(__file__).resolve().parent / "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import fx_agent_nuke_addon

    bridge = fx_agent_nuke_addon.NukeMCPServer
    if not hasattr(bridge, "_fx_original_dispatch"):
        bridge._fx_original_dispatch = bridge._run_in_nuke
    bridge._run_in_nuke = _dispatch_with_result
    fx_agent_nuke_addon.start(port=port)
