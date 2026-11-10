"""Wire operation allowlists shared by separate client and server packages."""

DATA_TOOLS = frozenset(
    [
        "fx_sync_module_baselines",
        "fx_get_module_catalog",
        "fx_get_module_knowledge",
        "fx_refresh_module_guides",
        "fx_get_production_rules",
        "fx_save_checkpoint",
        "fx_get_checkpoint",
        "fx_get_context",
        "fx_archive_case",
        "fx_get_case",
        "fx_validate_case",
        "fx_submit_experience",
        "fx_search_memory",
        "fx_get_memory",
        "fx_record_user_rule",
        "fx_get_user_rule",
        "fx_record_feedback",
        "fx_get_learning_history",
        "fx_get_feedback",
        "fx_finalize_learning",
        "fx_dream_memory",
        "fx_undo_dream",
        "fx_get_dream_runs",
        "fx_get_import_records",
        "fx_resolve_import_memory",
        "fx_report_render_issue",
        "fx_get_render_issue",
        "fx_list_render_issues",
        "fx_update_render_issue",
        "fx_plan_effect_study",
        "fx_get_effect_learning_queue",
        "fx_refine_effect",
        "fx_plan_template_import",
        "fx_learn_templates",
        "fx_search_templates",
        "fx_get_template",
        "fx_validate_template",
        "fx_export_template",
        "fx_learn_template_experience",
        "fx_annotate_template_effects",
        "fx_merge_effect_cards",
        "fx_search_effects",
        "fx_get_effect",
        "fx_review_template_effects",
    ]
)

TASK_METHODS = frozenset(
    [
        "begin",
        "get",
        "update",
        "plan",
        "question",
        "finish",
        "authorize",
        "edit",
        "invalidate",
        "observe",
    ]
)


KNOWLEDGE_WRITES = {
    "fx_learn_templates",
    "fx_annotate_template_effects",
    "fx_refine_effect",
    "fx_merge_effect_cards",
    "fx_learn_template_experience",
    "fx_export_template",
}
