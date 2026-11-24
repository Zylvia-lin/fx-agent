"""MCP tool-surface profile filtering to reduce host schema bloat safely."""

import os

from fx_agent.task_tools import BRIEF_TOOLS, GATE_TOOLS

TOOL_PROFILES = frozenset({"text_only", "full", "production", "learning"})

# Tools never removed regardless of profile so gating, status, and briefs always work.
PROTECTED_TOOLS = frozenset(
    GATE_TOOLS
    | BRIEF_TOOLS
    | {
        "fx_status",
        "fx_check_production_path",
        "fx_search_published_templates",
        "get_workflow_guide",
    }
)

# Legacy project-archiving, static template ingestion and effect-card tools hidden in `text_only` profile.
TEXT_ONLY_HIDDEN_FX_TOOLS = frozenset(
    {
        "fx_archive_case",
        "fx_get_case",
        "fx_validate_case",
        "fx_plan_template_import",
        "fx_learn_templates",
        "fx_search_templates",
        "fx_get_template",
        "fx_validate_template",
        "fx_export_template",
        "fx_learn_template_experience",
        "fx_plan_effect_study",
        "fx_get_effect_learning_queue",
        "fx_annotate_template_effects",
        "fx_merge_effect_cards",
        "fx_refine_effect",
        "fx_search_effects",
        "fx_get_effect",
        "fx_review_template_effects",
    }
)

# Low-frequency template ingestion/annotation/maintenance tools hidden in `production` profile.
PRODUCTION_HIDDEN_FX_TOOLS = frozenset(
    {
        "fx_sync_module_baselines",
        "fx_refresh_module_guides",
        "fx_dream_memory",
        "fx_undo_dream",
        "fx_get_dream_runs",
        "fx_import_data",
        "fx_get_import_records",
        "fx_resolve_import_memory",
        "fx_plan_template_import",
        "fx_learn_templates",
        "fx_validate_template",
        "fx_learn_template_experience",
        "fx_plan_effect_study",
        "fx_get_effect_learning_queue",
        "fx_annotate_template_effects",
        "fx_merge_effect_cards",
        "fx_refine_effect",
        "fx_review_template_effects",
    }
)

# Live render/handoff tools hidden in `learning` profile.
LEARNING_HIDDEN_FX_TOOLS = frozenset(
    {
        "fx_render_view_start",
        "fx_render_view_status",
        "fx_render_view_save",
        "fx_render_view_stop",
        "fx_report_render_issue",
        "fx_get_render_issue",
        "fx_list_render_issues",
        "fx_update_render_issue",
        "fx_save_checkpoint",
        "fx_get_checkpoint",
    }
)

# Optional upstream Houdini sub-domain groups hideable via FX_AGENT_HOUDINI_HIDE_GROUPS.
HOUDINI_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "chop": frozenset(
        {
            "get_chop_data",
            "create_chop_node",
            "list_chop_channels",
            "export_chop_to_parm",
        }
    ),
    "cop": frozenset(
        {
            "get_cop_info",
            "get_cop_geometry",
            "get_cop_layer",
            "create_cop_node",
            "set_cop_flags",
            "list_cop_node_types",
            "get_cop_vdb",
        }
    ),
    "top": frozenset(
        {
            "get_top_network_info",
            "cook_top_node",
            "cancel_top_cook",
            "pause_top_cook",
            "dirty_work_items",
            "get_work_item_states",
            "get_work_item_info",
            "get_pdg_graph",
            "generate_static_items",
            "get_top_scheduler_info",
            "get_failed_work_items",
            "get_top_logs",
        }
    ),
    "hda": frozenset(
        {
            "list_installed_hdas",
            "get_hda_info",
            "install_hda",
            "uninstall_hda",
            "reload_hda",
            "create_hda",
            "update_hda",
            "get_hda_sections",
            "get_hda_section_content",
            "set_hda_section_content",
            "list_hda_versions",
            "set_hda_interface",
            "edit_hda_interface",
        }
    ),
    "usd": frozenset(
        {
            "get_stage_info",
            "get_usd_prim",
            "list_usd_prims",
            "get_usd_attribute",
            "get_usd_attributes",
            "get_usd_world_transform",
            "get_usd_layers",
            "get_usd_prim_stats",
            "get_last_modified_prims",
            "create_lop_node",
            "set_usd_attribute",
            "get_usd_bound_material",
            "get_usd_materials",
            "find_usd_prims",
            "get_usd_composition",
            "get_usd_variants",
            "inspect_usd_layer",
        }
    ),
    "takes": frozenset(
        {
            "list_takes",
            "get_current_take",
            "set_current_take",
            "create_take",
        }
    ),
    "shelf": frozenset(
        {
            "list_shelf_tools",
            "get_shelf_tool_script",
            "run_shelf_tool",
        }
    ),
}


def resolve_tool_profile(profile: str | None = None) -> str:
    raw = (
        os.getenv("FX_AGENT_TOOL_PROFILE", "text_only") if profile is None else profile
    ).strip().lower()
    if not raw:
        return "text_only"
    if raw not in TOOL_PROFILES:
        raise ValueError(
            f"Unsupported FX_AGENT_TOOL_PROFILE '{raw}'; expected one of {sorted(TOOL_PROFILES)}"
        )
    return raw


def parse_hidden_groups(hide_groups: str | None = None) -> set[str]:
    raw = os.getenv("FX_AGENT_HOUDINI_HIDE_GROUPS", "") if hide_groups is None else hide_groups
    groups = {item.strip().lower() for item in raw.split(",") if item.strip()}
    unknown = groups - set(HOUDINI_TOOL_GROUPS)
    if unknown:
        raise ValueError(
            f"Unknown FX_AGENT_HOUDINI_HIDE_GROUPS: {sorted(unknown)}; "
            f"supported: {sorted(HOUDINI_TOOL_GROUPS)}"
        )
    return groups


def apply_tool_profile(
    mcp,
    *,
    software: str,
    profile: str | None = None,
    hide_groups: str | None = None,
) -> dict:
    """Remove tools outside the requested profile from a FastMCP server's tool manager."""
    active_profile = resolve_tool_profile(profile)
    hidden: set[str] = set()
    if active_profile == "text_only":
        hidden.update(TEXT_ONLY_HIDDEN_FX_TOOLS)
    elif active_profile == "production":
        hidden.update(PRODUCTION_HIDDEN_FX_TOOLS)
    elif active_profile == "learning":
        hidden.update(LEARNING_HIDDEN_FX_TOOLS)
    if software == "houdini":
        for group in parse_hidden_groups(hide_groups):
            hidden.update(HOUDINI_TOOL_GROUPS[group])
    hidden -= PROTECTED_TOOLS

    tools_map = getattr(mcp._tool_manager, "_tools", None)
    stash = getattr(mcp._tool_manager, "_fx_profile_stash", None)
    if not isinstance(stash, dict):
        stash = {}
        mcp._tool_manager._fx_profile_stash = stash
    removed: list[str] = []
    if isinstance(tools_map, dict):
        # Restore previously profile-hidden tools before applying the active profile.
        for name, tool in list(stash.items()):
            if name not in hidden and name not in tools_map:
                tools_map[name] = tool
                stash.pop(name, None)
        for name in sorted(hidden):
            if name in tools_map:
                stash[name] = tools_map.pop(name)
                removed.append(name)
            elif name in stash:
                removed.append(name)
    return {
        "profile": active_profile,
        "software": software,
        "removed": removed,
        "remaining": len(tools_map) if isinstance(tools_map, dict) else 0,
    }
