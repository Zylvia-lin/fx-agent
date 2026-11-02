"""Fixed project path rules must survive learned recipes and batch operations."""

import importlib
from unittest.mock import AsyncMock

import pytest

CACHE = "$HIP/geo/$OS/$OS.$F4.bgeo.sc"


@pytest.mark.parametrize(
    "path", ["$HIP/assets/a.bgeo.sc", "${HIP}/tex/a.<UDIM>.exr", "$HIP/a.$F4.bgeo.sc"]
)
def test_project_relative_reads_are_accepted(path):
    policy = importlib.import_module("fx_agent.production")
    assert policy.check_path(path, "read")["valid"]


@pytest.mark.parametrize(
    "path",
    [
        "D:/assets/a.bgeo",
        "../assets/a.bgeo",
        "$JOB/a.bgeo",
        "$HIPNAME/a.bgeo",
        "$HIP/../outside.bgeo",
        "$HIP/assets/../../outside.bgeo",
        "$HIP/`chs('x')`",
        "￥HIP/a.bgeo",
    ],
)
def test_absolute_external_and_unevaluable_reads_are_rejected(path):
    policy = importlib.import_module("fx_agent.production")
    assert not policy.check_path(path, "read")["valid"]


def test_cache_template_is_exact_and_rules_are_not_mutable():
    policy = importlib.import_module("fx_agent.production")
    assert policy.check_path(CACHE, "filecache")["valid"]
    assert not policy.check_path("$HIP/geo/file.$F.bgeo.sc", "filecache")["valid"]
    first = policy.production_rules()
    first["filecache"] = "D:/bad"
    assert policy.production_rules()["filecache"] == CACHE


@pytest.mark.asyncio
async def test_build_guard_rejects_file_paths_before_any_scene_mutation():
    policy = importlib.import_module("fx_agent.production")
    upstream = AsyncMock(return_value={"success": True})
    with pytest.raises(ValueError, match="HIP"):
        await policy.guard_command(
            "graph.build_network",
            {
                "parent_path": "/obj/fx",
                "nodes": [
                    {"type": "box", "name": "box1"},
                    {"type": "file", "parms": {"file": "D:/outside.bgeo"}},
                ],
            },
            upstream,
        )
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_guard_sets_supported_explicit_filecache_parameters_without_mutating_request():
    policy = importlib.import_module("fx_agent.production")
    original = {
        "parent_path": "/obj/fx",
        "nodes": [{"type": "filecache::2.0", "name": "smoke_cache"}],
    }
    prepared = await policy.guard_command("graph.build_network", original, AsyncMock())
    assert prepared["nodes"][0]["parms"] == {"filemethod": "explicit", "file": CACHE}
    assert "parms" not in original["nodes"][0]
    with pytest.raises(ValueError):
        await policy.guard_command(
            "graph.build_network",
            {
                "nodes": [{"type": "filecache::2.0", "parms": {"file": "D:/wrong"}}],
            },
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_existing_node_parameter_guard_uses_actual_node_type():
    policy = importlib.import_module("fx_agent.production")
    upstream = AsyncMock(return_value={"type": {"name": "file", "category": "Sop"}})
    with pytest.raises(ValueError):
        await policy.guard_command(
            "parameters.set_parameters",
            {
                "node_path": "/obj/fx/file1",
                "params": {"file": "$JOB/wrong.bgeo"},
            },
            upstream,
        )
    prepared = await policy.guard_command(
        "parameters.set_parameter",
        {
            "node_path": "/obj/fx/file1",
            "parm_name": "file",
            "value": "$HIP/assets/a.bgeo",
        },
        upstream,
    )
    assert prepared["value"].startswith("$HIP/")


@pytest.mark.asyncio
async def test_bridge_adapter_enforces_rules_and_passes_original_timeout(monkeypatch):
    policy = importlib.import_module("fx_agent.production")
    call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("fxhoudinimcp.bridge.HoudiniBridge.execute", call)
    bridge = policy.ProductionBridge()
    result = await bridge.execute(
        "graph.build_network",
        {
            "nodes": [{"type": "filecache::2.0"}],
        },
        timeout=999,
    )
    assert result == {"ok": True}
    assert call.call_args.args[1]["nodes"][0]["parms"]["file"] == CACHE
    assert call.call_args.args[2] == 999


@pytest.mark.asyncio
async def test_alias_expressions_tuple_values_and_sop_chain_cannot_bypass_rules():
    policy = importlib.import_module("fx_agent.production")
    for command, params in (
        (
            "graph.build_network",
            {
                "nodes": [
                    {
                        "type": "filecache::2.0",
                        "expressions": {},
                        "exprs": {"file": '"D:/outside.bgeo.sc"'},
                    }
                ]
            },
        ),
        (
            "parameters.set_parameter",
            {"node_path": "/obj/f/file1", "parm_name": "file", "value": ["D:/outside.bgeo"]},
        ),
        (
            "workflow.build_sop_chain",
            {"steps": [{"type": "file", "params": {"file": "D:/outside.bgeo"}}]},
        ),
    ):
        with pytest.raises(ValueError):
            await policy.guard_command(command, params, AsyncMock())
    built = await policy.guard_command(
        "workflow.build_sop_chain",
        {
            "steps": [{"type": "filecache::2.0", "name": "cache"}],
        },
        AsyncMock(),
    )
    assert built["steps"][0]["params"]["file"] == CACHE


@pytest.mark.asyncio
async def test_cache_write_checks_actual_output_not_only_template_knobs():
    policy = importlib.import_module("fx_agent.production")
    actual = "D:/outside/cache.0001.bgeo.sc"

    async def read(command, params):
        if command == "nodes.get_node_info":
            return {"type": {"name": "filecache::2.0"}}
        if command == "scene.get_scene_info":
            return {"hip_file": "D:/project/scene.hip", "current_frame": 1}
        return {
            "filemethod": {"value": 1},
            "file": {"raw_value": CACHE},
            "sopoutput": {"value": actual},
        }[params["parm_name"]]

    with pytest.raises(ValueError, match="sopoutput"):
        await policy.guard_command("cache.write_cache", {"node_path": "/obj/f/cache"}, read)
    actual = "D:/project/geo/cache/cache.0001.bgeo.sc"
    assert (await policy.guard_command("cache.write_cache", {"node_path": "/obj/f/cache"}, read))[
        "node_path"
    ]


@pytest.mark.asyncio
async def test_native_simulation_helpers_normalize_created_filecache(monkeypatch):
    policy = importlib.import_module("fx_agent.production")
    call = AsyncMock(
        side_effect=[
            {"cache_path": "/obj/f/cache"},
            {"type": {"name": "filecache::2.0"}},
            {"success": True},
            {"value": 1},
            {"raw_value": CACHE},
        ]
    )
    monkeypatch.setattr("fxhoudinimcp.bridge.HoudiniBridge.execute", call)
    await policy.ProductionBridge().execute("workflow.setup_pyro_sim", {"name": "smoke"})
    assert call.await_args_list[2].args[1]["params"] == {"filemethod": "explicit", "file": CACHE}


@pytest.mark.asyncio
async def test_simulation_helper_does_not_hide_parameter_write_failures(monkeypatch):
    policy = importlib.import_module("fx_agent.production")
    call = AsyncMock(
        side_effect=[
            {"success": True, "cache_path": "/obj/f/cache"},
            {"type": {"name": "filecache::2.0"}},
            {"errors": ["file is locked"]},
        ]
    )
    monkeypatch.setattr("fxhoudinimcp.bridge.HoudiniBridge.execute", call)
    with pytest.raises(ValueError, match="场景已创建"):
        await policy.ProductionBridge().execute("workflow.setup_pyro_sim", {})


@pytest.mark.asyncio
async def test_incompatible_import_helper_fails_before_mutation_with_alternative():
    policy = importlib.import_module("fx_agent.production")
    with pytest.raises(ValueError, match="build_network"):
        await policy.guard_command(
            "scene.import_file", {"file_path": "$HIP/assets/a.bgeo"}, AsyncMock()
        )
