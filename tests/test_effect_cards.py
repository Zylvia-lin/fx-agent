"""Each visible variant remains independently searchable, even with identical graphs."""

import json
import shutil
from pathlib import Path

import pytest
from test_templates import hip, learn, nk

from fx_agent.data_import import import_data, import_records
from fx_agent.effect_cards import EffectLibrary
from fx_agent.migration import migrate_data
from fx_agent.store import Store, compact
from fx_agent.templates import TemplateLibrary


def study(tmp_path, software="houdini"):
    source = hip(tmp_path / "effect.hip") if software == "houdini" else nk(tmp_path / "comp.nk")
    library = TemplateLibrary(tmp_path / "data")
    result = learn(library, source)["items"][0]
    assert result["state"] == "imported", result
    return EffectLibrary(library.root), result["study_id"], source


def card(**changes):
    return {
        "effect_key": "body-flow",
        "title": "角色身体贴身流动能量粒子",
        "scheme_key": "surface-emit-noise-trail",
        "scheme_name": "表面发射、扰动与拖尾",
        "method": "从角色表面生成点，扰动速度后生成拖尾。",
        "subject": "角色身体、人物表面",
        "appearance": "身体表面附着细小发光粒子和能量丝线。",
        "motion": "沿身体轮廓缓慢流动。",
        "aliases": ["人物身体能量粒子", "character body energy particles"],
        "tags": ["角色", "身体", "能量", "粒子", "贴身"],
        "node_paths": ["/obj/geo1/source", "/obj/geo1/shape"],
        "parameter_notes": "scale 控制变形幅度；具体值参考原始参数。",
        "animation_notes": "参考 shape.chn 原始曲线。",
        "render_notes": "发光外观尚未通过预览核实。",
        "compositing_notes": "可在 Nuke 中进一步控制辉光，属于建议。",
        "variant_notes": "本卡描述贴身流动，与同方案的向外爆发变体不同。",
        "scope": "工程当前设置；其他参数组合未验证。",
        "reuse_steps": [
            "导出原工程工作副本",
            "检查角色输入和列出的节点",
            "在工作副本上调整速度与渲染",
        ],
        "inputs": ["角色几何输入"],
        "limitations": "外观解释为推断，不能认定已经复现。",
        "basis": "inferred",
        "evidence_note": "节点和参数来自静态快照，外观与运动描述需要成片确认。",
        "modules": ["particles", "sop"],
    } | changes


def test_same_scheme_same_nodes_retains_multiple_effect_variants(tmp_path):
    library, study_id, source = study(tmp_path)
    before = source.read_bytes()
    burst = card(
        effect_key="body-burst",
        title="角色身体向外爆发能量粒子",
        motion="从身体向四周高速喷射。",
        variant_notes="共用相同方案；速度幅度和发射时序不同。",
        animation_notes="爆发式发射曲线。",
        render_notes="建议更强运动模糊，尚未验证。",
    )
    result = library.annotate(study_id, [card(), burst])
    assert len(result["items"]) == 2
    assert len({i["card_id"] for i in result["items"]}) == 2
    assert library.search(scheme_key=card()["scheme_key"])["total"] == 2
    found = library.search("我要做一个角色身体能量粒子")
    assert found["total"] == 2
    assert "角色身体" in found["items"][0]["title"]
    for item in result["items"]:
        detail = library.get(item["card_id"])
        assert Path(detail["markdown_path"]).is_file()
        assert "渲染差异" in Path(detail["markdown_path"]).read_text(encoding="utf-8")
        assert detail["card"]["study_id"] == study_id
    assert source.read_bytes() == before


def test_render_only_variant_and_alias_search_do_not_collapse(tmp_path):
    library, study_id, _ = study(tmp_path)
    flow = card()
    glow = card(
        effect_key="body-glow",
        title="角色轮廓强辉光能量丝",
        aliases=["character outline glow"],
        render_notes="同一几何，使用更强发光与不同曝光。",
        variant_notes="仅渲染表现不同，节点方案与运动共用。",
    )
    library.annotate(study_id, [flow, glow])
    assert library.search("character outline glow")["items"][0]["effect_key"] == "body-glow"
    assert library.search(scheme_key=flow["scheme_key"])["total"] == 2
    assert library.search("不存在的词xyz")["items"] == []


def test_revisions_are_idempotent_stale_protected_and_never_delete_variants(tmp_path):
    library, study_id, _ = study(tmp_path)
    first = library.annotate(study_id, [card()])["items"][0]
    assert library.annotate(study_id, [card()])["items"][0]["state"] == "unchanged"
    with pytest.raises(ValueError, match="current"):
        library.annotate(study_id, [card(motion="不同运动")])
    revised = library.annotate(
        study_id, [card(motion="更缓慢的贴身流动", supersedes=[first["card_id"]])]
    )["items"][0]
    assert library.get(revised["card_id"])["card"]["parent_ids"] == [first["card_id"]]
    assert library.search()["total"] == 1
    assert library.search(include_history=True)["total"] == 2
    assert library.get(first["card_id"])["card"]["motion"] == card()["motion"]


def test_nuke_and_houdini_cards_share_search_but_keep_software_and_nodes(tmp_path):
    library, houdini_id, _ = study(tmp_path)
    project = nk(tmp_path / "comp.nk")
    nuke_id = learn(TemplateLibrary(library.root), project)["items"][0]["study_id"]
    library.annotate(houdini_id, [card()])
    nuke_card = card(
        effect_key="energy-glow",
        title="角色能量粒子辉光合成",
        scheme_key="grade-glow-composite",
        scheme_name="调色与辉光合成",
        node_paths=["/Controls/Grade1"],
        modules=["compositing", "color"],
        variant_notes="Nuke合成阶段控制粒子外观。",
    )
    library.annotate(nuke_id, [nuke_card])
    assert {i["software"] for i in library.search("角色能量粒子")["items"]} == {"houdini", "nuke"}
    assert library.search(software="nuke")["total"] == 1
    assert library.search(module="particles")["total"] == 1


def test_evidence_and_node_refs_are_checked_without_promoting_to_success(tmp_path):
    library, study_id, _ = study(tmp_path)
    with pytest.raises(ValueError, match="node"):
        library.annotate(study_id, [card(node_paths=["/fake/node"])])
    with pytest.raises(ValueError, match="preview"):
        library.annotate(study_id, [card(basis="preview", preview_paths=["previews/fake.png"])])
    with pytest.raises(ValueError, match="quote"):
        library.annotate(study_id, [card(basis="user")])
    item = library.annotate(
        study_id, [card(basis="user", user_quote="这个工程展示了角色身体能量粒子")]
    )["items"][0]
    assert library.get(item["card_id"])["card"]["knowledge_status"] == "reference"
    assert (
        TemplateLibrary(library.root).submit_experience(
            study_id,
            title="能量粒子",
            method="使用此方案",
            reason="工程显示此结构",
            applies_when="相同输入",
            modules=["particles"],
            effect_card_id=item["card_id"],
        )["decision"]
        == "candidate"
    )


def test_coverage_requires_explicit_review_and_becomes_stale_after_card_edit(tmp_path):
    library, study_id, _ = study(tmp_path)
    assert library.review(study_id)["state"] == "not_annotated"
    definition = card(
        effect_family=["粒子"],
        effect_kind="身体能量粒子",
        behaviors=["流动"],
        naming_basis="user",
        basis="user",
        user_quote="角色身体贴身流动能量粒子",
    )
    item = library.annotate(study_id, [definition])["items"][0]
    assert library.review(study_id)["state"] == "needs_review"
    with pytest.raises(ValueError, match="reviewed"):
        library.review(
            study_id,
            reviewed_node_paths=["/obj/geo1/source"],
            expected_card_ids=[item["card_id"]],
            exclusions=[{"node_path": "/obj/geo1", "reason": "容器"}],
        )
    media = tmp_path / "media"
    media.mkdir()
    done = library.review(
        study_id,
        reviewed_node_paths=["/obj/geo1", "/obj/geo1/source", "/obj/geo1/shape"],
        expected_card_ids=[item["card_id"]],
        exclusions=[{"node_path": "/obj/geo1", "reason": "技术容器，本身不构成单独效果"}],
        note="已检查工程当前存在的效果分支；没有穷举参数空间。",
        media_source=str(media),
    )
    assert done["state"] == "reviewed"
    library.annotate(study_id, [card(motion="重新描述运动", supersedes=[item["card_id"]])])
    assert library.review(study_id)["state"] == "needs_review"


def test_card_text_and_search_are_paginated_with_real_budget(tmp_path):
    library, study_id, _ = study(tmp_path)
    item = library.annotate(study_id, [card(appearance="能量丝线" * 400)])["items"][0]
    detail = library.get(item["card_id"], max_chars=1024)
    assert detail["detail_required"] and len(compact(detail)) <= 1024
    first = library.get(item["card_id"], section="text", max_chars=1024)
    assert first["next_offset"] > 0 and len(compact(first)) <= 1024
    second = library.get(
        item["card_id"], section="text", offset=first["next_offset"], max_chars=1024
    )
    assert second["next_offset"] > first["next_offset"]
    results = library.search(max_chars=1024)
    assert results["items"] and len(compact(results)) <= 1024


def test_cards_and_reviews_migrate_merge_and_rebuild_text_without_original(tmp_path):
    library, study_id, source = study(tmp_path)
    item = library.annotate(study_id, [card()])["items"][0]
    migrated = tmp_path / "migrated"
    migrate_data(library.root, migrated)
    source.unlink()
    shutil.rmtree(library.root)
    target = tmp_path / "target"
    preview = import_data(migrated, target)
    assert preview["counts"]["template_effects"]["added"] == 1
    assert not target.exists()
    import_data(migrated, target, apply=True)
    imported = EffectLibrary(target)
    assert imported.search("角色身体能量粒子")["items"]
    assert Path(imported.get(item["card_id"])["markdown_path"]).read_text(encoding="utf-8")
    assert import_data(migrated, target, apply=True)["counts"]["template_effects"]["skipped"] == 1


def test_card_branches_survive_merge_and_require_all_heads_to_resolve(tmp_path):
    library, study_id, _ = study(tmp_path)
    original = library.annotate(study_id, [card()])["items"][0]
    other_root = tmp_path / "other"
    migrate_data(library.root, other_root)
    other = EffectLibrary(other_root)
    a = library.annotate(study_id, [card(motion="分支A", supersedes=[original["card_id"]])])[
        "items"
    ][0]
    b = other.annotate(study_id, [card(motion="分支B", supersedes=[original["card_id"]])])["items"][
        0
    ]
    import_data(other.root, library.root, apply=True)
    assert library.search()["total"] == 2
    assert all(i["has_conflict"] for i in library.search()["items"])
    with pytest.raises(ValueError, match="current"):
        library.annotate(study_id, [card(motion="解决", supersedes=[a["card_id"]])])
    library.annotate(
        study_id, [card(motion="保留双方证据后的描述", supersedes=[a["card_id"], b["card_id"]])]
    )
    assert library.search()["total"] == 1


def test_identical_independent_labels_are_equivalent_but_not_different_variants(tmp_path):
    library, study_id, _ = study(tmp_path)
    other_root = tmp_path / "other"
    migrate_data(library.root, other_root)
    first = library.annotate(study_id, [card()])["items"][0]
    second = EffectLibrary(other_root).annotate(study_id, [card()])["items"][0]
    import_data(other_root, library.root, apply=True)
    catalog = library.catalog(study_id)
    assert catalog["card_count"] == 1 and not catalog["has_conflict"]
    found = library.search()["items"]
    assert len(found) == 1 and found[0]["equivalent_count"] == 2
    assert not found[0]["has_conflict"]
    assert set(library.get(first["card_id"])["equivalent_card_ids"]) == {
        first["card_id"],
        second["card_id"],
    }
    assert library.annotate(study_id, [card()])["items"][0]["state"] == "unchanged"
    library.annotate(study_id, [card(effect_key="same-method-new-variant")])
    assert library.search()["total"] == 2


def test_conflicting_study_does_not_activate_its_incoming_cards(tmp_path):
    library, study_id, _ = study(tmp_path)
    other_root = tmp_path / "other"
    migrate_data(library.root, other_root)
    other = EffectLibrary(other_root)
    other.annotate(study_id, [card()])
    with Store(other_root).connection() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM template_studies WHERE study_id=?", (study_id,)
            ).fetchone()[0]
        )
        payload["title"] = "conflicting source metadata"
        db.execute(
            "UPDATE template_studies SET payload=? WHERE study_id=?", (compact(payload), study_id)
        )
    result = import_data(other_root, library.root, apply=True)
    assert result["counts"]["template_effects"]["conflicts"] == 1
    assert not library.search()["items"]
    assert import_records(library.root, result["import_id"], table="template_effects")["items"]


@pytest.mark.asyncio
async def test_effect_mcp_tools_and_template_search_integration(tmp_path, monkeypatch):
    from fx_agent.server import get_server
    from fx_agent.task_gates import TaskGates, TaskSpec

    library, study_id, _ = study(tmp_path)
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(library.root))
    server = get_server()
    task = TaskGates(library.root).begin(
        TaskSpec(
            project_id="p",
            shot_id="s",
            software="houdini",
            kind="learning",
            scene_path=str(tmp_path / "effect.hip"),
            endpoint="127.0.0.1:18186",
            goal="学习效果",
            acceptance=["效果卡有来源"],
            budget="只读",
            modules=["particles"],
        )
    )
    from test_task_gates import evidence

    gates = TaskGates(library.root)
    evidence(gates, task["task_id"])
    gates.plan(
        task["task_id"],
        task["revision"],
        "只读学习",
        "来源工程",
        "标注当前效果",
        [],
        "没有既有案例",
    )
    tools = {t.name: t for t in await server.list_tools()}
    for name in (
        "fx_annotate_template_effects",
        "fx_search_effects",
        "fx_get_effect",
        "fx_review_template_effects",
        "fx_plan_effect_study",
        "fx_get_effect_learning_queue",
        "fx_refine_effect",
        "fx_merge_effect_cards",
    ):
        assert name in tools
    result = await server.call_tool(
        "fx_annotate_template_effects",
        {"study_id": study_id, "cards": [card()], "fx_task_id": task["task_id"]},
    )
    assert json.loads(result[0].text)["items"]
    search = await server.call_tool("fx_search_templates", {"query": "角色身体能量粒子"})
    assert json.loads(search[0].text)["effect_matches"]["items"]
    detail = await server.call_tool("fx_get_template", {"study_id": study_id})
    assert json.loads(detail[0].text)["effect_catalog"]["card_count"] == 1
    queue = await server.call_tool("fx_get_effect_learning_queue", {})
    assert json.loads(queue[0].text)["items"][0]["study_id"] == study_id


def test_merge_layers_previews_then_preserves_sources_and_search(tmp_path):
    library, study_id, source = study(tmp_path)
    before = source.read_bytes()
    originals = library.annotate(
        study_id,
        [
            card(effect_key="eye-one", title="一号龙眼部亮点"),
            card(effect_key="eye-two", title="二号龙眼部亮点"),
        ],
    )["items"]
    ids = [r["card_id"] for r in originals]
    definition = card(
        effect_key="dragon-eyes",
        title="龙眼橙黄能量亮点",
        aliases=["一号龙眼部亮点", "二号龙眼部亮点"],
    )
    preview = library.merge(ids, definition, "同一效果的两个对象层")
    assert preview["applied"] is False
    assert library.search()["total"] == 2
    assert preview["card"]["component_card_ids"] == sorted(ids)
    result = library.merge(ids, definition, "同一效果的两个对象层", apply=True)
    merged_id = result["items"][0]["card_id"]
    assert library.search()["total"] == 1
    assert library.catalog(study_id)["card_count"] == 1
    assert library.search("二号龙眼部亮点")["items"][0]["card_id"] == merged_id
    detail = library.get(merged_id)
    assert set(detail["card"]["component_card_ids"]) == set(ids)
    assert detail["card"]["visual_verified"] is False
    assert set(library.get(ids[0])["merged_into_card_ids"]) == {merged_id}
    assert library.get(ids[0])["current_card_ids"] == []
    assert library.get(ids[0])["card"]["title"] == "一号龙眼部亮点"
    assert library.search(include_history=True)["total"] == 3
    assert (
        library.merge(ids, definition, "同一效果的两个对象层", apply=True)["items"][0]["state"]
        == "unchanged"
    )
    assert source.read_bytes() == before
    revised = library.refine(
        merged_id, {"motion": "跟随龙头闪动"}, ["effect_card:" + merged_id], "补充运动"
    )["items"][0]
    assert library.search()["total"] == 1
    assert library.get(revised["card_id"])["card"]["component_card_ids"] == sorted(ids)
    with pytest.raises(ValueError, match="merged"):
        library.refine(ids[0], {"motion": "旧层不能独立复活"}, ["effect_card:" + ids[0]], "修改")


def test_merge_rejects_stale_cross_study_and_incomplete_details(tmp_path):
    library, study_id, _ = study(tmp_path)
    rows = library.annotate(study_id, [card(), card(effect_key="other")])["items"]
    ids = [r["card_id"] for r in rows]
    with pytest.raises(ValueError, match="node"):
        library.merge(ids, card(effect_key="merged", node_paths=[]), "不可丢节点", apply=True)
    assert library.search()["total"] == 2
    library.refine(ids[0], {"motion": "新版本"}, ["effect_card:" + ids[0]], "修订")
    with pytest.raises(ValueError, match="current"):
        library.merge(ids, card(effect_key="merged"), "旧卡", apply=True)
    other = nk(tmp_path / "other.nk")
    other_study = learn(TemplateLibrary(library.root), other)["items"][0]["study_id"]
    other_id = library.annotate(
        other_study, [card(node_paths=["/Controls/Grade1"], modules=["compositing"])]
    )["items"][0]["card_id"]
    with pytest.raises(ValueError, match="same study"):
        library.merge([ids[1], other_id], card(effect_key="merged"), "跨工程", apply=True)


def test_merged_effect_survives_data_import(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(study_id, [card(effect_key="one"), card(effect_key="two")])[
            "items"
        ]
    ]
    merged = library.merge(ids, card(effect_key="merged"), "同效果分层", apply=True)["items"][0]
    destination = tmp_path / "destination"
    import_data(library.root, destination, apply=True)
    other = EffectLibrary(destination)
    assert other.search()["total"] == 1
    assert other.get(merged["card_id"])["card"]["component_card_ids"] == sorted(ids)
    assert other.get(ids[0])["merged_into_card_ids"] == [merged["card_id"]]


def test_merge_search_keeps_original_names_without_manual_aliases(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(
            study_id,
            [
                card(effect_key="one", title="DragonEyeAlpha", aliases=["unique-eye-alpha"]),
                card(effect_key="two", title="DragonEyeBeta", aliases=["unique-eye-beta"]),
            ],
        )["items"]
    ]
    merged = library.merge(ids, card(effect_key="merged", aliases=[]), "同类对象层", apply=True)
    assert library.search("unique-eye-beta")["items"][0]["card_id"] == merged["items"][0]["card_id"]


def test_merge_preview_checks_conflicting_heads(tmp_path):
    library, study_id, _ = study(tmp_path)
    originals = library.annotate(study_id, [card(), card(effect_key="other")])["items"]
    other_root = tmp_path / "other"
    migrate_data(library.root, other_root)
    first = originals[0]["card_id"]
    a = library.refine(first, {"motion": "分支A"}, ["effect_card:" + first], "A")["items"][0]
    EffectLibrary(other_root).refine(first, {"motion": "分支B"}, ["effect_card:" + first], "B")
    import_data(other_root, library.root, apply=True)
    with pytest.raises(ValueError, match="all current heads"):
        library.merge(
            [a["card_id"], originals[1]["card_id"]], card(effect_key="merged"), "不完整头"
        )


def test_incoming_merge_does_not_hide_divergent_local_component(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(study_id, [card(), card(effect_key="other")])["items"]
    ]
    other_root = tmp_path / "other"
    migrate_data(library.root, other_root)
    other = EffectLibrary(other_root)
    other.merge(ids, card(effect_key="merged"), "对象层", apply=True)
    original = library._record_card(ids[0])
    with Store(library.root).connection() as db:
        original["title"] = "local divergent evidence"
        db.execute(
            "UPDATE template_effects SET payload=? WHERE card_id=?", (compact(original), ids[0])
        )
    result = import_data(other_root, library.root, apply=True)
    assert library.search()["total"] == 2
    assert result["counts"]["template_effects"]["conflicts"] == 2
    report = import_records(library.root, result["import_id"], table="template_effects")
    assert "component_card_conflict" in {r["reason"] for r in report["items"]}


def test_nested_merge_keeps_old_layers_retired_and_tracks_current_total(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(
            study_id, [card(effect_key="one"), card(effect_key="two"), card(effect_key="three")]
        )["items"]
    ]
    merged = library.merge(ids[:2], card(effect_key="first-total"), "同类层", apply=True)["items"][
        0
    ]
    total = library.merge(
        [merged["card_id"], ids[2]], card(effect_key="final-total"), "同类层", apply=True
    )["items"][0]
    with pytest.raises(ValueError, match="merged"):
        library.refine(ids[0], {"motion": "不能复活"}, ["effect_card:" + ids[0]], "修改")
    assert library.search()["total"] == 1
    assert library.get(ids[0])["merged_into_card_ids"] == [total["card_id"]]
    assert library.get(ids[0])["current_card_ids"] == []


def test_merged_questions_can_be_resolved_with_later_evidence(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(
            study_id, [card(effect_key="one", questions=["用途待确认"]), card(effect_key="two")]
        )["items"]
    ]
    merged = library.merge(
        ids, card(effect_key="merged", questions=["用途待确认"]), "同类层", apply=True
    )["items"][0]
    revised = library.refine(
        merged["card_id"], {"questions": []}, ["effect_card:" + merged["card_id"]], "根据新证据解疑"
    )["items"][0]
    assert library.get(revised["card_id"])["card"]["questions"] == []
    assert library.get(ids[0])["card"]["questions"] == ["用途待确认"]


def test_later_imported_source_revision_marks_merge_for_review(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(study_id, [card(), card(effect_key="other")])["items"]
    ]
    other_root = tmp_path / "other"
    migrate_data(library.root, other_root)
    library.merge(ids, card(effect_key="merged"), "同类层", apply=True)
    EffectLibrary(other_root).refine(
        ids[0], {"motion": "另一设备的新观察"}, ["effect_card:" + ids[0]], "新观察"
    )
    import_data(other_root, library.root, apply=True)
    found = library.search()
    assert found["total"] == 2
    assert all(r["has_conflict"] for r in found["items"])
    assert library.catalog(study_id)["has_conflict"] is True


def test_merge_retains_module_search_when_workflow_spans_over_four_modules(tmp_path):
    library, study_id, _ = study(tmp_path)
    ids = [
        r["card_id"]
        for r in library.annotate(
            study_id,
            [
                card(effect_key="production", modules=["io", "sop", "rendering"]),
                card(effect_key="shader", modules=["materials", "solaris", "rendering"]),
            ],
        )["items"]
    ]
    definition = card(effect_key="workflow", modules=["sop", "materials", "rendering", "solaris"])
    result = library.merge(ids, definition, "同一效果的制作与渲染", apply=True)
    merged_id = result["items"][0]["card_id"]
    detail = library.get(merged_id)
    assert set(detail["card"]["component_modules"]) == {
        "io",
        "sop",
        "rendering",
        "materials",
        "solaris",
    }
    assert library.search(module="io")["items"][0]["card_id"] == merged_id
    assert library.search(module="solaris")["items"][0]["card_id"] == merged_id
    revised = library.refine(
        merged_id, {"motion": "补充运动"}, ["effect_card:" + merged_id], "新观察"
    )["items"][0]
    assert library.search(module="io")["items"][0]["card_id"] == revised["card_id"]
