"""Effect identity, render evidence and non-destructive second-pass learning."""

import pytest
from test_effect_cards import card, study
from test_templates import cpio, learn

from fx_agent.archive import CaseArchive
from fx_agent.data_import import import_data
from fx_agent.effect_cards import EffectLibrary
from fx_agent.template_extract import extract_project
from fx_agent.templates import TemplateLibrary


def identified(**changes):
    return card(
        **(
            {
                "effect_family": ["粒子"],
                "effect_kind": "能量粒子",
                "behaviors": ["环绕", "吸引"],
                "naming_basis": "user",
                "basis": "user",
                "user_quote": "这是粒子环绕吸引效果",
            }
            | changes
        )
    )


def test_render_study_finds_labels_outputs_sequences_and_movies(tmp_path):
    from fx_agent.effect_study import EffectStudy

    source = tmp_path / "source"
    source.mkdir()
    project = source / "energy.hip"
    project.write_bytes(
        cpio(
            {
                "obj/energy.init": "type = geo\n",
                "obj/render.init": "type = geo\n",
                "obj/render/merge.init": "type = object_merge\n",
                "obj/render/merge.parm": 'objpath1 ( "/obj/energy" )\n',
                "out/mantra.init": "type = ifd\n",
                "out/mantra.parm": 'vm_picture ( "$HIP/render/energy.$F4.exr" )\nforceobject ( "/obj/render" )\nf1 ( 1 )\nf2 ( 4 )\n',
                "obj/box.netboxinit": 'comment := "能量";\n',
            }
        )
    )
    (source / "render").mkdir()
    for frame in (1, 2, 4):
        (source / "render" / f"energy.{frame:04d}.exr").write_bytes(b"not an image")
    (source / "final.mp4").write_bytes(b"not a movie")
    library = TemplateLibrary(tmp_path / "data")
    study_id = learn(library, project)["items"][0]["study_id"]
    before = project.read_bytes()
    plan = EffectStudy(library.root).plan(study_id, str(source))
    assert any(x.get("text") == "能量" for x in plan["annotations"])
    output = next(x for x in plan["render_outputs"] if x["node_path"] == "/out/mantra")
    assert "/obj/energy" in output["related_node_paths"]
    assert output["frame_parameters"]["f2"] == "4"
    sequence = next(x for x in plan["media"] if x["kind"] == "sequence")
    assert sequence["missing_frame_count"] == 1
    assert sequence["missing_ranges"] == [[3, 3]]
    assert sequence["sample_frames"] == [1, 2, 4]
    assert sequence["render_node_paths"] == ["/out/mantra"]
    assert sequence["expected_frame_checks"][0]["missing_frame_count"] == 1
    movie = next(x for x in plan["media"] if x["kind"] == "movie")
    assert movie["pairing"] == "unassigned"
    assert plan["visual_analysis"] == "required_host_inspection"
    assert project.read_bytes() == before
    focused = EffectStudy(library.root).plan(study_id, str(source), node_path="/obj/energy")
    assert focused["focus_render_node_paths"] == ["/out/mantra"]


def test_nuke_labels_are_preserved_without_assuming_backdrop_membership():
    snapshot = extract_project(
        b'BackdropNode {\n name Energy\n label "energy"\n}\nStickyNote {\n name note\n label "orbit"\n}\n',
        "nuke",
    )
    assert snapshot["netboxes"][0]["label"] == "energy"
    assert snapshot["sticky_notes"][0]["text"] == "orbit"


def test_draft_questions_block_completion_and_survive_revision(tmp_path):
    library, study_id, _ = study(tmp_path)
    first = library.annotate(study_id, [identified(questions=["是否向中心收拢？"])])["items"][0]
    catalog = library.catalog(study_id)
    assert catalog["learning_state"] == "needs_confirmation"
    with pytest.raises(ValueError, match="identity|question|evidence"):
        library.review(
            study_id,
            reviewed_node_paths=["/obj/geo1", "/obj/geo1/source", "/obj/geo1/shape"],
            expected_card_ids=[first["card_id"]],
            exclusions=[{"node_path": "/obj/geo1", "reason": "容器"}],
        )
    revised = library.annotate(study_id, [identified(supersedes=[first["card_id"]])])["items"][0]
    assert library.get(first["card_id"])["card"]["questions"] == ["是否向中心收拢？"]
    assert library.get(revised["card_id"])["card"]["parent_ids"] == [first["card_id"]]


def test_legacy_cards_remain_searchable_but_need_second_pass(tmp_path):
    from fx_agent.effect_study import EffectStudy

    library, study_id, _ = study(tmp_path)
    first = library.annotate(study_id, [card()])["items"][0]
    assert library.search("粒子")["items"][0]["identity_status"] == "needs_relearning"
    queue = EffectStudy(library.root).queue()
    assert queue["items"][0]["study_id"] == study_id
    assert queue["items"][0]["learning_state"] == "needs_relearning"
    assert library.get(first["card_id"])["card"]["title"] == card()["title"]


def test_compound_query_does_not_call_orbit_only_a_complete_match(tmp_path):
    library, study_id, _ = study(tmp_path)
    library.annotate(
        study_id,
        [
            identified(),
            identified(
                effect_key="orbit-only",
                behaviors=["环绕"],
                motion="围绕中心旋转，半径保持不变。",
                title="粒子持续环绕",
                aliases=[],
                variant_notes="只有环绕运动。",
            ),
        ],
    )
    results = library.search("粒子环绕吸引")["items"]
    assert results[0]["effect_key"] == card()["effect_key"]
    assert results[0]["match_kind"] == "complete"
    partial = next(x for x in results if x["effect_key"] == "orbit-only")
    assert partial["match_kind"] == "partial"
    assert "吸引" in partial["missing_concepts"]


def test_visual_evidence_needs_archived_samples_and_source_nodes(tmp_path):
    library, study_id, _ = study(tmp_path)
    with pytest.raises(ValueError, match="evidence|preview|sample"):
        library.annotate(
            study_id,
            [
                identified(
                    visual_evidence=[
                        {
                            "kind": "sequence",
                            "source_path": "render/energy.$F4.exr",
                            "render_node_path": "/obj/geo1",
                            "frame_range": "1-24",
                            "observation": "粒子旋转并收拢",
                            "sample_paths": ["previews/not-there.png"],
                        }
                    ]
                )
            ],
        )


def test_review_requires_supplied_media_to_be_accounted_for(tmp_path):
    library, study_id, _ = study(tmp_path)
    item = library.annotate(study_id, [identified()])["items"][0]
    media = tmp_path / "media"
    media.mkdir()
    movie = media / "final.mp4"
    movie.write_bytes(b"not decoded")
    args = dict(
        reviewed_node_paths=["/obj/geo1", "/obj/geo1/source", "/obj/geo1/shape"],
        expected_card_ids=[item["card_id"]],
        exclusions=[{"node_path": "/obj/geo1", "reason": "容器"}],
        media_source=str(media),
    )
    with pytest.raises(ValueError, match="media"):
        library.review(study_id, **args)
    report = library.review(
        study_id,
        **args,
        media_reviews=[
            {
                "path": str(movie),
                "status": "unavailable",
                "note": "无法解码，需要用户补充可播放文件",
            }
        ],
    )
    assert report["learning_state"] == "needs_confirmation"
    assert report["state"] != "reviewed"


def test_dream_surfaces_cards_and_revision_records_learning_provenance(tmp_path):
    from fx_agent.learning import LearningStore

    library, study_id, _ = study(tmp_path)
    first = library.annotate(study_id, [card()])["items"][0]
    plan = LearningStore(library.root).dream("", "preview")
    assert plan["effect_learning"]["queue_tool"] == "fx_get_effect_learning_queue"
    assert plan["effect_learning"]["merge_tool"] == "fx_merge_effect_cards"
    revised = library.annotate(
        study_id,
        [
            identified(
                supersedes=[first["card_id"]],
                refinement_sources=["effect_card:" + first["card_id"]],
                refinement_reason="用户补充实际运动，更新效果识别",
            )
        ],
    )["items"][0]
    stored = library.get(revised["card_id"])["card"]
    assert stored["refinement_sources"] == ["effect_card:" + first["card_id"]]
    assert library.get(first["card_id"])["identity_status"] == "needs_relearning"
    with pytest.raises(ValueError, match="source"):
        library.annotate(
            study_id,
            [
                identified(
                    supersedes=[revised["card_id"]],
                    refinement_sources=["dream:" + "0" * 32],
                    refinement_reason="无效来源",
                )
            ],
        )


def test_old_card_can_gain_new_independently_archived_visual_evidence(tmp_path):
    library, study_id, source = study(tmp_path)
    first = library.annotate(study_id, [card()])["items"][0]
    sample = tmp_path / "sample.png"
    sample.write_bytes(b"test sample; host is responsible for visual inspection")
    archive = CaseArchive(library.root).create(
        project_id="p",
        shot_id="s",
        title="补充画面",
        files=[{"path": str(sample), "role": "preview"}],
    )
    revised = library.refine(
        first["card_id"],
        updates={
            "title": "粒子环绕并向中心收拢",
            "effect_family": ["粒子"],
            "effect_kind": "能量粒子",
            "behaviors": ["环绕", "吸引"],
            "naming_basis": "preview",
            "basis": "preview",
            "visual_evidence": [
                {
                    "kind": "movie",
                    "source_path": "final.mp4",
                    "time_range": "1-3s",
                    "observation": "粒子旋转同时半径缩小",
                    "case_id": archive["case_id"],
                    "sample_paths": [archive["files"][0]["archive_path"]],
                }
            ],
        },
        sources=["effect_card:" + first["card_id"]],
        reason="查看补充视频后识别效果",
    )
    new_id = revised["items"][0]["card_id"]
    assert library.get(new_id)["identity_status"] == "identified"
    assert library.get(new_id)["card"]["method"] == card()["method"]
    assert library.get(first["card_id"])["identity_status"] == "needs_relearning"
    with pytest.raises(ValueError, match="current"):
        library.refine(
            first["card_id"], {"title": "过期更新"}, ["effect_card:" + first["card_id"]], "过期"
        )
    target = tmp_path / "target"
    import_data(library.root, target, apply=True)
    assert (
        EffectLibrary(target).get(new_id)["card"]["visual_evidence"][0]["case_id"]
        == archive["case_id"]
    )
    assert source.exists()


def test_latest_media_review_and_migration_preserve_failure(tmp_path):
    from fx_agent.effect_study import EffectStudy

    library, study_id, _ = study(tmp_path)
    item = library.annotate(study_id, [identified()])["items"][0]
    media = tmp_path / "media"
    media.mkdir()
    args = dict(
        reviewed_node_paths=["/obj/geo1", "/obj/geo1/source", "/obj/geo1/shape"],
        expected_card_ids=[item["card_id"]],
        exclusions=[{"node_path": "/obj/geo1", "reason": "容器"}],
        media_source=str(media),
    )
    assert library.review(study_id, **args)["state"] == "reviewed"
    movie = media / "final.mp4"
    movie.write_bytes(b"cannot decode")
    assert (
        library.review(
            study_id,
            **args,
            media_reviews=[
                {"path": str(movie), "status": "unavailable", "note": "无法解码，请用户补充"}
            ],
        )["state"]
        != "reviewed"
    )
    target = tmp_path / "merged"
    import_data(library.root, target, apply=True)
    assert EffectLibrary(target).catalog(study_id)["learning_state"] == "needs_confirmation"
    assert EffectStudy(target).queue()["items"][0]["media_issues"]


def test_unknown_query_requirements_are_not_complete_matches(tmp_path):
    library, study_id, _ = study(tmp_path)
    library.annotate(study_id, [identified()])
    result = library.search("particle orbit attract backwards red")["items"][0]
    assert result["match_kind"] != "complete"
    assert set(result["unverified_terms"]) == {"backwards", "red"}


def test_plan_and_queue_pagination_validate_bounds(tmp_path):
    from fx_agent.effect_study import EffectStudy
    from fx_agent.store import compact

    library, study_id, _ = study(tmp_path)
    service = EffectStudy(library.root)
    assert len(compact(service.plan(study_id, max_chars=1024))) <= 1024
    assert len(compact(service.queue(max_chars=1024))) <= 1024
    with pytest.raises(ValueError):
        service.plan(study_id, node_path="/missing")
    with pytest.raises(ValueError):
        service.queue(offset=-1)
