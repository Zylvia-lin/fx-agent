"""Shared fixed rules, independent of any DCC transport."""

CACHE_PATH = "$HIP/geo/$OS/$OS.$F4.bgeo.sc"


def production_rules(software: str = "houdini") -> dict:
    """Return a fresh rule card; learned content has no write access to these rules."""
    if software == "maya":
        return {
            "version": 1,
            "software": "maya",
            "priority": "current_user_instruction_then_fixed_rules_then_learned_recipes",
            "target": "Explicit live PID/session/scene selection; never guess a window",
            "scene_api": "Maya main thread only",
            "paths": "Use an explicit named .ma/.mb task target and existing absolute HDRI paths",
            "renderers": "Inspect loaded plugin versions and actual node attributes; no automatic install or replacement",
            "timeout": "Inspect actual scene after timeout; never automatically replay writes or renders",
            "verification": "Saved scene, structural check, actual rendered image and independent archive; user acceptance remains separate",
        }
    if software == "nuke":
        return {
            "version": 1,
            "software": "nuke",
            "priority": "current_user_instruction_then_fixed_rules_then_learned_recipes",
            "target": "Explicit live PID/session/script selection; never guess a Nuke window",
            "scene_api": "Nuke main thread only; arbitrary execute_python is disabled on both MCP and bridge",
            "paths": "Keep Read/Write and .nk paths explicitly verified; do not overwrite unconfirmed outputs",
            "graph": "Inspect selection and existing node graph before creating, connecting, or routing nodes",
            "verification": "Confirmed save_script, find_broken_reads check, actual viewer capture inspection, and independent archive; user acceptance remains separate",
        }
    return {
        "version": 1,
        "software": "houdini",
        "read_root": "$HIP/",
        "filecache": CACHE_PATH,
        "filecache_mode": "explicit",
        "priority": "current_user_instruction_then_fixed_rules_then_learned_recipes",
        "asset_import": "Copy external assets into the project, then set $HIP-relative paths.",
        "guard_scope": "common filename parameters, graph builds, File/File Cache creation and cache writes",
        "limitations": "Not a sandbox: arbitrary Python, callbacks, HDAs and manual UI require a separate scene audit.",
    }
