"""Read-only, bounded template learning and portable revision history."""

import json
import shutil
import threading
from pathlib import Path

import pytest

from fx_agent.archive import CaseArchive, file_hash
from fx_agent.data_import import import_data
from fx_agent.memory import MemoryStore
from fx_agent.migration import migrate_data
from fx_agent.templates import TemplateLibrary


def cpio(sections):
    result = bytearray()
    for name, body in [*sections.items(), ("TRAILER!!!", "")]:
        name, body = name.encode() + b"\0", body.encode()
        header = b"070707" + b"000000" * 7 + b"00000000000"
        result.extend(header + f"{len(name):06o}{len(body):011o}".encode() + name + body)
    return bytes(result)


def hip(path, value="0.2"):
    path.write_bytes(
        cpio(
            {
                "obj/geo1.init": "type = geo\n",
                "obj/geo1/source.init": "type = sphere\n",
                "obj/geo1/shape.init": "type = attribwrangle\n",
                "obj/geo1/shape.parm": '{\nversion 1\nsnippet ( "@P *= ch(\\"scale\\");" )\nscale ( '
                + value
                + " )\n}\n",
                "obj/geo1/shape.def": 'inputs\n{\n0 source 0 0\n}\ncomment "形态控制"\nposition 2 3\n',
                "obj/geo1/shape.chn": "channel scale\n{\nkey 1 0\nkey 24 1\n}\n",
            }
        )
    )
    return path


def nk(path, value="1"):
    path.write_text(
        """version 15.2 v1
Root {
 name sample.nk
}
Read {
 inputs 0
 file "plates/beauty.%04d.exr"
 name Read1
}
set N1 [stack 0]
Group {
 name Controls
}
 Input {
  inputs 0
  name Input1
 }
 Grade {
  name Grade1
  multiply {"""
        + value
        + """}
  label "制作说明\\n保留"
  xpos 20
  ypos 40
  onCreate {python {raise RuntimeError("must never execute")}}
 }
end_group
push $N1
Merge2 {
 inputs 2
 name Merge1
 mix {{curve x1 0 x24 1}}
}
""",
        encoding="utf-8",
    )
    return path


def learn(library, source, **kwargs):
    return library.ingest(str(source), project_id="p", shot_id="s", **kwargs)


def test_mixed_inventory_prunes_backups_and_groups_sequences(tmp_path):
    sources = tmp_path / "source"
    sources.mkdir()
    hip(sources / "effect.hiplc")
    nk(sources / "comp.nk")
    (sources / "BACKUP").mkdir()
    hip(sources / "BACKUP/old.hip")
    (sources / "comp.nk~").write_text("backup")
    (sources / "motion.abc").write_bytes(b"large cache")
    for i in range(1, 11):
        (sources / f"beauty.{i:04}.exr").write_bytes(b"frame")
    root = tmp_path / "data"
    report = TemplateLibrary(root).plan(str(sources))
    assert not root.exists(), "Preview must not create a DB or data files"
    assert len(report["projects"]) == 2
    assert len(report["sequences"]) == 1
    assert report["sequences"][0]["count"] == 10
    assert report["cache_files"] == 1
    assert report["copy_policy"]["archive_caches"] is False


def test_hip_learning_retains_source_raw_animation_and_deduplicates(tmp_path):
    source = hip(tmp_path / "effect.hiplc")
    before = file_hash(source)
    library = TemplateLibrary(tmp_path / "data")
    first = learn(library, source, modules=["sop"])["items"][0]
    assert first["state"] == "imported"
    detail = library.get(first["study_id"], section="snapshot")
    snapshot = detail["content"]
    shape = next(n for n in snapshot["nodes"] if n["path"].endswith("/shape"))
    assert shape["parameters"]["scale"] == "0.2"
    assert "key 24 1" in shape["raw_sections"]["chn"]
    assert "position 2 3" in shape["raw_sections"]["def"]
    assert snapshot["connections"][0]["src_path"] == "/obj/geo1/source"
    assert file_hash(source) == before
    duplicate = learn(library, source, modules=["sop"])["items"][0]
    assert duplicate["state"] == "unchanged"
    assert duplicate["study_id"] == first["study_id"]
    found = library.search("形态控制")["items"]
    assert found and found[0]["study_id"] == first["study_id"]
    assert MemoryStore(library.root).search(query="形态")["items"] == []
    patterns = library.get(first["study_id"], section="patterns")["content"]
    assert {p["type"] for p in patterns} == {"scene", "subgraph", "recipe"}


def test_nuke_groups_curves_stack_and_callbacks_are_only_text(tmp_path):
    source = nk(tmp_path / "comp.nk")
    before = source.read_bytes()
    library = TemplateLibrary(tmp_path / "data")
    item = learn(library, source)["items"][0]
    snapshot = library.get(item["study_id"], section="snapshot")["content"]
    grade = next(n for n in snapshot["nodes"] if n["path"] == "/Controls/Grade1")
    assert "must never execute" in grade["parameters"]["onCreate"]
    merge = next(n for n in snapshot["nodes"] if n["name"] == "Merge1")
    assert "curve x1 0 x24 1" in merge["parameters"]["mix"]
    assert any(
        c["src_path"] == "/Read1" and c["dst_path"] == "/Merge1" for c in snapshot["connections"]
    )
    assert source.read_bytes() == before
    assert snapshot["execution"] == "static_only"


def test_assets_are_selective_and_budget_is_enforced(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    project = nk(source / "comp.nk")
    (source / "plates").mkdir()
    for i in range(1, 21):
        (source / "plates" / f"beauty.{i:04d}.exr").write_bytes(b"render")
    cache = source / "motion.abc"
    cache.write_bytes(b"cache")
    preview = source / "preview.mov"
    preview.write_bytes(b"preview")
    library = TemplateLibrary(tmp_path / "data")
    item = learn(
        library,
        project,
        files=[
            {"path": str(cache), "role": "dependency"},
            {"path": str(preview), "role": "preview"},
        ],
    )["items"][0]
    detail = library.get(item["study_id"])
    case = CaseArchive(library.root).get(detail["case_id"], "p")
    assert any(f["source_path"] == str(preview) for f in case["files"])
    assert not any(f["source_path"].endswith((".abc", ".exr")) for f in case["files"])
    assert any(d["status"] == "sequence_not_archived" for d in detail["dependencies"])
    assert any(s["reason"] == "cache_not_archived" for s in detail["omitted_files"])
    small = learn(library, project, policy={"max_total_bytes": 10})
    assert small["items"][0]["state"] == "failed"


def test_changed_source_creates_revision_and_candidate_links_to_case(tmp_path):
    source = hip(tmp_path / "effect.hip")
    library = TemplateLibrary(tmp_path / "data")
    first = learn(library, source)["items"][0]
    hip(source, "0.8")
    second = learn(library, source)["items"][0]
    assert second["study_id"] != first["study_id"]
    assert library.get(second["study_id"])["parent_ids"] == [first["study_id"]]
    result = library.submit_experience(
        second["study_id"],
        title="控制形态",
        method="用 scale 控制形态",
        reason="工程使用此参数",
        applies_when="相同输入",
        modules=["sop"],
    )
    assert result["decision"] == "candidate"
    memory = MemoryStore(library.root).get(result["memory_id"])
    assert memory["case_id"] == library.get(second["study_id"])["case_id"]
    assert second["study_id"] in memory["source_ref"]
    assert library.get(first["study_id"], section="snapshot")["content"]["nodes"]


def test_template_candidate_then_accepted_result_updates_module_knowledge(tmp_path):
    from fx_agent.learning import LearningStore
    from fx_agent.module_guides import ModuleGuides

    source = hip(tmp_path / "effect.hip")
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"explicit user accepted preview fixture")
    library = TemplateLibrary(tmp_path / "data")
    item = learn(
        library, source, modules=["sop"], files=[{"path": str(preview), "role": "preview"}]
    )["items"][0]
    candidate = library.submit_experience(
        item["study_id"],
        title="模板形态控制",
        method="通过scale控制形态",
        reason="静态工程设置",
        applies_when="相同结构",
        modules=["sop"],
    )
    store = LearningStore(library.root)
    assert store.search(query="形态控制")["items"] == []
    saved = store.record_feedback(
        project_id="p",
        shot_id="s",
        modules=["sop"],
        effect_type="形态控制",
        attempt_id="template-use",
        problem="需要控制变形幅度",
        change="使用模板scale控制",
        reason="保留原始输入关系",
        result="用户确认当前工作副本结果",
        outcome="accepted",
        user_quote="保留这个方案",
        source_ref="template_study:" + item["study_id"],
        case_id=item["case_id"],
        expected_revision=0,
    )
    learned = store.finalize(
        saved["feedback_id"], "p", "使用scale控制变形幅度", "保持输入关系", "同结构同尺度"
    )
    assert learned["decision"] == "admitted"
    assert item["study_id"] in store.get(learned["memory_id"])["source_ref"]
    assert store.get(candidate["memory_id"])["status"] == "candidate"
    assert learned["module_guides"]["state"] == "updated"
    assert "scale" in json.dumps(ModuleGuides(library.root).get(module="sop"), ensure_ascii=False)


def test_portable_migration_and_idempotent_merge_without_originals(tmp_path):
    source = hip(tmp_path / "effect.hip")
    library = TemplateLibrary(tmp_path / "A")
    item = learn(library, source)["items"][0]
    migrated = tmp_path / "B"
    assert item["state"] == "imported", item
    migrate_data(library.root, migrated)
    source.unlink()
    shutil.rmtree(library.root)
    relocated = TemplateLibrary(migrated)
    assert relocated.get(item["study_id"], section="snapshot")["content"]["nodes"]
    assert relocated.validate(item["study_id"])["valid"]
    destination = tmp_path / "C"
    assert import_data(migrated, destination)["counts"]["template_studies"]["added"] == 1
    assert not destination.exists()
    import_data(migrated, destination, apply=True)
    assert TemplateLibrary(destination).search("attribwrangle")["items"]
    repeated = import_data(migrated, destination, apply=True)
    assert repeated["counts"]["template_studies"]["skipped"] == 1
    export = tmp_path / "reuse"
    TemplateLibrary(destination).export(item["study_id"], str(export))
    assert (export / "effect.hip").is_file()
    with pytest.raises(FileExistsError):
        TemplateLibrary(destination).export(item["study_id"], str(export))


def test_partial_bad_file_does_not_stop_batch_and_retry_resumes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    hip(source / "good.hip")
    (source / "unknown.hiplc").write_bytes(b"unknown encrypted format")
    library = TemplateLibrary(tmp_path / "data")
    first = learn(library, source)
    assert {i["state"] for i in first["items"]} == {"imported", "partial"}
    partial = next(i for i in first["items"] if i["state"] == "partial")
    assert library.get(partial["study_id"])["limitations"]
    assert library.validate(partial["study_id"])["valid"]
    again = learn(library, source)
    assert all(i["state"] == "unchanged" for i in again["items"])


def test_corrupted_archive_blocks_read_export_and_migration(tmp_path):
    source = hip(tmp_path / "effect.hip")
    library = TemplateLibrary(tmp_path / "A")
    item = learn(library, source)["items"][0]
    detail = library.get(item["study_id"])
    case = CaseArchive(library.root).get(detail["case_id"], "p")
    snapshot = next(f for f in case["files"] if f["source_path"].endswith("snapshot.json"))
    (Path(case["case_path"]) / snapshot["archive_path"]).write_text("{}")
    assert not library.validate(item["study_id"])["valid"]
    with pytest.raises(ValueError, match="checksum"):
        library.get(item["study_id"], section="snapshot")
    with pytest.raises(ValueError):
        migrate_data(library.root, tmp_path / "B")


def test_preview_requires_no_model_and_rejects_data_source_overlap(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    hip(root / "effect.hip")
    with pytest.raises(ValueError, match="overlap"):
        TemplateLibrary(root).plan(str(tmp_path))


def test_same_filename_in_different_origins_is_not_a_revision(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    library = TemplateLibrary(tmp_path / "data")
    first = learn(library, hip(a / "effect.hip"))["items"][0]
    second = learn(library, hip(b / "effect.hip", "2"))["items"][0]
    assert first["source_id"] != second["source_id"]
    assert library.get(second["study_id"])["parent_ids"] == []


def test_stable_key_follows_moved_project_and_relative_texture(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    project = a / "comp.nk"
    project.write_text("Read {\n inputs 0\n file texture.hdr\n name Read1\n}\n")
    (a / "texture.hdr").write_bytes(b"HDR")
    library = TemplateLibrary(tmp_path / "data")
    first = learn(library, project, template_key="portable")["items"][0]
    shutil.copytree(a, b)
    second = learn(library, b / project.name, template_key="portable")["items"][0]
    assert second["state"] == "unchanged"
    assert second["study_id"] == first["study_id"]


def test_batch_limit_is_enforced_and_resume_skips_previous_copies(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(3):
        hip(source / f"effect{i}.hip")
    library = TemplateLibrary(tmp_path / "data")
    single = learn(library, source / "effect0.hip")["items"][0]
    limit = single["archive_bytes"] + 1
    result = learn(library, source, policy={"max_batch_bytes": limit})
    assert result["new_archive_bytes"] <= limit
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["failed"] == 1
    again = learn(library, source, policy={"max_batch_bytes": limit})
    assert again["counts"]["unchanged"] == 2
    assert again["new_archive_bytes"] <= limit


def test_write_output_is_not_auto_archived_and_static_texture_is(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    project = source / "comp.nk"
    project.write_text(
        "Read {\n inputs 0\n file {texture.hdr}\n name Read1\n}\nWrite {\n file output.png\n name Write1\n}\n"
    )
    (source / "texture.hdr").write_bytes(b"hdr")
    (source / "output.png").write_bytes(b"output")
    library = TemplateLibrary(tmp_path / "data")
    item = learn(library, project)["items"][0]
    case = CaseArchive(library.root).get(item["case_id"], "p")
    sources = {Path(f["source_path"]).name for f in case["files"]}
    assert "texture.hdr" in sources
    assert "output.png" not in sources
    assert any(
        d["status"] == "output_not_archived" for d in library.get(item["study_id"])["dependencies"]
    )


def test_source_change_rejected_before_case_publication(tmp_path, monkeypatch):
    import fx_agent.templates as templates

    source = hip(tmp_path / "effect.hip")
    original = templates.extract_project

    def mutate(data, software):
        result = original(data, software)
        hip(source, "changed")
        return result

    monkeypatch.setattr(templates, "extract_project", mutate)
    library = TemplateLibrary(tmp_path / "data")
    result = learn(library, source)
    assert result["items"][0]["state"] == "failed"
    assert "changed" in result["items"][0]["error"]
    assert not list((library.root / "cases").glob("*/manifest.json"))
    assert not list((library.root / "cases/.staging").iterdir())
    assert library.search()["items"] == []


def test_history_and_small_budget_pagination_never_hide_current_revision(tmp_path):
    from fx_agent.store import compact

    source = hip(tmp_path / "effect.hip")
    library = TemplateLibrary(tmp_path / "data")
    ids = []
    for value in range(5):
        hip(source, str(value))
        item = learn(library, source)["items"][0]
        assert item["state"] == "imported", item
        ids.append(item["study_id"])
    assert library.search()["items"][0]["study_id"] == ids[-1]
    assert library.search()["total"] == 1
    page = library.search(include_history=True, max_chars=1024)
    assert page["items"] and page["next_offset"] > 0
    assert len(compact(page)) <= 1024
    record = library.get(ids[-1])
    assert record["parent_ids"] == [ids[-2]]


def test_large_nodes_can_be_read_as_budgeted_text_and_metadata_pages(tmp_path):
    from fx_agent.store import compact

    source = tmp_path / "large.nk"
    source.write_text(
        'NoOp {\n name Notes\n label "' + "工程说明" * 2000 + '"\n}\n', encoding="utf-8"
    )
    library = TemplateLibrary(tmp_path / "data")
    item = learn(library, source)["items"][0]
    page = library.get(item["study_id"], section="snapshot", max_chars=1024)
    assert page["truncated"] and len(compact(page)) <= 1024
    node = library.get(item["study_id"], section="nodes", max_chars=1024)
    assert node["detail_required"]
    text = library.get(item["study_id"], section="snapshot_text", limit=500, max_chars=1024)
    assert text["next_offset"] > 0 and len(compact(text)) <= 1024


def test_merge_preserves_branched_revisions_and_import_conflicts(tmp_path):
    from fx_agent.data_import import import_records
    from fx_agent.store import Store, compact

    source = hip(tmp_path / "effect.hip")
    a, b = TemplateLibrary(tmp_path / "A"), TemplateLibrary(tmp_path / "B")
    first = learn(a, source)["items"][0]
    migrate_data(a.root, b.root)
    hip(source, "2")
    a_new = learn(a, source)["items"][0]
    hip(source, "3")
    b_new = learn(b, source)["items"][0]
    result = import_data(b.root, a.root, apply=True)
    assert result["counts"]["template_studies"]["added"] == 1
    assert {x["study_id"] for x in a.search()["items"]} == {a_new["study_id"], b_new["study_id"]}
    with Store(b.root).connection() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM template_studies WHERE study_id=?", (first["study_id"],)
            ).fetchone()[0]
        )
        payload["title"] = "other device changed metadata"
        db.execute(
            "UPDATE template_studies SET payload=? WHERE study_id=?",
            (compact(payload), first["study_id"]),
        )
    conflict = import_data(b.root, a.root, apply=True)
    assert conflict["counts"]["template_studies"]["conflicts"] == 1
    assert a.get(first["study_id"])["title"] != payload["title"]
    records = import_records(a.root, conflict["import_id"], table="template_studies")
    assert records["items"][0]["incoming"]["payload"] != records["items"][0]["local"]["payload"]


def test_equivalent_independent_studies_collapse_after_merge(tmp_path):
    source = hip(tmp_path / "effect.hip")
    a, b = TemplateLibrary(tmp_path / "A"), TemplateLibrary(tmp_path / "B")
    learn(a, source, template_key="shared")
    learn(b, source, template_key="shared")
    import_data(b.root, a.root, apply=True)
    found = a.search()
    assert found["total"] == 1
    assert found["items"][0]["equivalent_count"] == 2
    history = a.get(found["items"][0]["study_id"], section="history")
    assert len(history["content"]) == 2


def test_export_equivalent_external_assets_and_reserved_names(tmp_path):
    origin, external_a, external_b = tmp_path / "origin", tmp_path / "a", tmp_path / "b"
    for folder in (origin, external_a, external_b):
        folder.mkdir()
    source = hip(origin / "effect.hip")
    for folder in (external_a, external_b):
        (folder / "texture.hdr").write_bytes(b"same HDR")
    (origin / "study").mkdir()
    (origin / "study/snapshot.json").write_text("original metadata")
    (origin / "template-manifest.json").write_text("original manifest")
    files = [
        {"path": str(folder / "texture.hdr"), "role": "dependency"}
        for folder in (external_a, external_b)
    ]
    files += [
        {"path": str(origin / name), "role": "dependency"}
        for name in ("study/snapshot.json", "template-manifest.json")
    ]
    library = TemplateLibrary(tmp_path / "data")
    item = learn(library, source, files=files)["items"][0]
    exported = library.export(item["study_id"], str(tmp_path / "work"))
    assert (tmp_path / "work/template-manifest.json").read_text() == "original manifest"
    assert (tmp_path / "work/study/snapshot.json").read_text() == "original metadata"
    assert Path(exported["manifest_path"]).is_file()
    assert len(list((tmp_path / "work/external").rglob("texture.hdr"))) == 1


def test_cancelled_import_leaves_source_and_no_published_case(tmp_path):
    from fx_agent.archive import ArchiveCancelled

    source = hip(tmp_path / "effect.hip")
    cancellation = threading.Event()
    cancellation.set()
    library = TemplateLibrary(tmp_path / "data")
    with pytest.raises(ArchiveCancelled):
        learn(library, source, cancellation=cancellation)
    assert not library.root.exists()


def test_directory_publication_retries_only_transient_windows_errors(tmp_path, monkeypatch):
    from fx_agent.archive import publish_directory

    stage, destination = tmp_path / "stage", tmp_path / "published"
    stage.mkdir()
    rename = Path.rename
    attempts = []

    def busy_once(path, target):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError("transient scanner lock")
            error.winerror = 5
            raise error
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", busy_once)
    publish_directory(stage, destination)
    assert destination.is_dir() and len(attempts) == 2

    def denied(path, target):
        raise PermissionError("not a transient Windows lock")

    monkeypatch.setattr(Path, "rename", denied)
    with pytest.raises(PermissionError):
        publish_directory(destination, tmp_path / "other")


def test_cpio_padding_and_unknown_nuke_commands_are_preserved():
    from fx_agent.template_extract import extract_project

    data = cpio({"obj/test.init": "type = geo\n"})
    boundary = data.index(b"070707", 6)
    data = data[:boundary] + b"\n\0" + data[boundary:]
    assert extract_project(data, "houdini")["nodes"]
    result = extract_project(b"unknown_command [python malicious]\nNoOp {\n name Safe\n}\n", "nuke")
    assert result["parse_status"] == "partial" and result["nodes"]


def test_nuke_layout_commands_and_comments_do_not_become_nodes_or_break_stack():
    from fx_agent.template_extract import extract_project

    data = b"""# comment containing an unmatched { must not affect the graph
define_window_layout_xml {<?xml version="1.0"?><layout/>}
add_layer {custom custom.r custom.g custom.b}
Read {
 inputs 0
 name Read1
}
NoOp {
 name Out
}
"""
    snapshot = extract_project(data, "nuke")
    assert snapshot["parse_status"] == "parsed"
    assert [n["name"] for n in snapshot["nodes"]] == ["Read1", "Out"]
    assert snapshot["connections"][0]["src_path"] == "/Read1"
    assert "define_window_layout_xml" in next(iter(snapshot["global_sections"].values()))


@pytest.mark.parametrize("data", [b"070707", b"070707" + b"x" * 76, b"NoOp {", b"end_group\n"])
def test_malformed_projects_are_partial_without_execution(data):
    from fx_agent.template_extract import extract_project

    software = "houdini" if data.startswith(b"070707") else "nuke"
    result = extract_project(data, software)
    assert result["parse_status"] == "partial" and result["limitations"]


def test_official_local_hip_is_read_without_houdini_or_source_changes(tmp_path):
    source = Path(
        "C:/Program Files/Side Effects Software/Houdini 22.0.466/houdini/help/examples/nodes/cop2/template.hip"
    )
    if not source.exists():
        pytest.skip("Optional installed official HIP fixture")
    before = file_hash(source)
    library = TemplateLibrary(tmp_path / "data")
    result = learn(library, source)["items"][0]
    assert result["state"] == "imported" and result["node_count"] > 0
    assert file_hash(source) == before
    assert library.validate(result["study_id"])["valid"]


@pytest.mark.asyncio
async def test_template_tools_are_registered_and_read_only_plan(tmp_path, monkeypatch):
    from fx_agent.server import get_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path / "data"))
    source = hip(tmp_path / "effect.hip")
    server = get_server()
    tools = {t.name: t for t in await server.list_tools()}
    for name in (
        "fx_plan_template_import",
        "fx_learn_templates",
        "fx_search_templates",
        "fx_get_template",
        "fx_validate_template",
        "fx_export_template",
        "fx_learn_template_experience",
    ):
        assert name in tools
    result = await server.call_tool("fx_plan_template_import", {"source": str(source)})
    assert json.loads(result[0].text)["projects"]
    assert not (tmp_path / "data").exists()


@pytest.mark.asyncio
async def test_mcp_template_learning_search_export_and_candidate(tmp_path, monkeypatch):
    from fx_agent.server import get_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path / "data"))
    source = hip(tmp_path / "effect.hip")
    server = get_server()

    from test_task_gates import evidence

    from fx_agent.task_gates import TaskGates, TaskSpec

    gates = TaskGates(tmp_path / "data")
    task = gates.begin(
        TaskSpec(
            project_id="p",
            shot_id="s",
            software="houdini",
            kind="learning",
            scene_path=str(source),
            endpoint="127.0.0.1:18186",
            goal="学习模板",
            acceptance=["归档提取并建立候选"],
            budget="静态学习",
            modules=["particles"],
        )
    )
    evidence(gates, task["task_id"])
    gates.plan(task["task_id"], 1, "只读提取", "测试工程", "记录候选", [], "无已有案例")

    async def call(name, arguments):
        arguments = arguments | {"fx_task_id": task["task_id"]}
        result = await server.call_tool(name, arguments)
        return json.loads(result[0].text)

    result = await call(
        "fx_learn_templates",
        {"source": str(source), "project_id": "p", "shot_id": "s", "modules": ["sop"]},
    )
    study = result["items"][0]["study_id"]
    assert (await call("fx_search_templates", {"query": "形态"}))["items"]
    assert (await call("fx_get_template", {"study_id": study, "section": "nodes"}))["content"]
    assert (await call("fx_validate_template", {"study_id": study}))["valid"]
    exported = await call(
        "fx_export_template", {"study_id": study, "destination": str(tmp_path / "work")}
    )
    assert Path(exported["destination"], "effect.hip").exists()
    candidate = await call(
        "fx_learn_template_experience",
        {
            "study_id": study,
            "title": "形态",
            "method": "scale控制形态",
            "reason": "工程采用",
            "applies_when": "同输入",
            "modules": ["sop"],
        },
    )
    assert candidate["decision"] == "candidate"


def test_cli_plan_and_import_use_explicit_data_root(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    source = nk(tmp_path / "comp.nk")
    command = [
        sys.executable,
        str(root / "scripts/learn_templates.py"),
        str(source),
        "--data-root",
        str(tmp_path / "data"),
    ]
    preview = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["projects"]
    assert not (tmp_path / "data").exists()
    imported = subprocess.run(
        command + ["--apply", "--project-id", "p", "--shot-id", "s"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["counts"]["imported"] == 1
