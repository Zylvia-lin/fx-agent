"""Explicit native-node layout and file-based copying; never drives the desktop."""

import tempfile
from pathlib import Path


def _integer(value):
    if type(value) is not int:
        raise ValueError("Graph coordinates and input indices must be finite integers")
    return value


def _toolset_path(nuke, node_class, explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute() or path.suffix.lower() != ".nk" or not path.is_file():
            raise ValueError("Toolset requires an existing absolute .nk path")
        return path.resolve()
    if not node_class or Path(node_class).name != node_class or any(c in node_class for c in "/\\:"):
        raise ValueError("Specify a class name, or an explicit toolset_path")
    roots = {Path(p).expanduser() for p in nuke.pluginPath()}
    roots.add(Path.home() / ".nuke" / "ToolSets")
    matches = {p.resolve() for root in roots if root.is_dir()
               for p in root.rglob("*.nk") if p.stem.casefold() == node_class.casefold()}
    if not matches:
        raise ValueError(f"No .nk toolset found for {node_class}; specify toolset_path")
    if len(matches) != 1:
        raise ValueError(f"Ambiguous toolset {node_class}; specify toolset_path: {sorted(map(str, matches))}")
    return matches.pop()


def _describe(node):
    return {"name": node.fullName(), "class": node.Class(),
            "xpos": node.xpos(), "ypos": node.ypos()}


def _configure(node, params):
    knobs = params.get("knobs") or {}
    missing = set(knobs) - set(node.knobs())
    if missing:
        raise ValueError(f"Unknown knobs: {sorted(missing)}")
    if params.get("name"):
        node.setName(params["name"])
    for key, value in knobs.items():
        node[key].setValue(value)


def _position(params):
    position = params.get("position")
    if position is not None:
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise ValueError("Position must contain x and y")
        return [_integer(v) for v in position]
    return None


def import_toolset(params):
    import nuke

    path = _toolset_path(nuke, "", params["path"])
    xy = _position(params)
    parent = nuke.thisGroup()
    before = list(parent.nodes())
    selection = [(node, node.isSelected()) for node in before]
    try:
        with parent:
            for node in before:
                node.setSelected(False)
            nuke.nodePaste(str(path))
            created = [node for node in parent.nodes() if node not in before]
            if not created:
                raise ValueError("Toolset imported no nodes")
            if len(created) != 1 and (params.get("single") or params.get("name") or params.get("knobs")):
                raise ValueError("Toolset requires one top-level node for create/name/knobs")
            if len(created) == 1:
                _configure(created[0], params)
            if xy is not None:
                left, top = min(n.xpos() for n in created), min(n.ypos() for n in created)
                for node in created:
                    node.setXYpos(node.xpos() - left + xy[0], node.ypos() - top + xy[1])
            for node in created:
                node.setSelected(False)
    except Exception as error:
        try:
            for node in reversed(parent.nodes()):
                if node not in before:
                    nuke.delete(node)
        except Exception as rollback_error:
            raise RuntimeError(f"Import failed: {error}; rollback failed: {rollback_error}") from error
        raise
    finally:
        for node, selected in selection:
            node.setSelected(selected)
    result = {"nodes": [_describe(node) for node in created], "source_path": str(path),
              "creation_method": "toolset"}
    if len(created) == 1:
        result.update(result["nodes"][0])
    return {"status": "ok", "result": result}


def create_node(params):
    import nuke

    xy = _position(params)
    parent = nuke.thisGroup()
    before = list(parent.nodes())
    selection = [(node, node.isSelected()) for node in before]
    try:
        node = nuke.createNode(params["node_class"], inpanel=False)
    except RuntimeError as error:
        # A callback may fail after creating nodes: never paste a second copy then.
        partial = [node for node in parent.nodes() if node not in before]
        try:
            for node in reversed(partial):
                nuke.delete(node)
        except Exception as rollback_error:
            raise RuntimeError(f"Creation failed: {error}; rollback failed: {rollback_error}") from error
        finally:
            for node, selected in selection:
                node.setSelected(selected)
        if partial:
            raise
        message = str(error).lower()
        if not any(text in message for text in ("unknown command", "unknown node", "unknown class")):
            raise
        path = _toolset_path(nuke, params["node_class"], params.get("toolset_path"))
        return import_toolset({**params, "path": str(path), "single": True})
    try:
        _configure(node, params)
        if xy is not None:
            node.setXYpos(*xy)
    except Exception:
        nuke.delete(node)
        for previous, selected in selection:
            previous.setSelected(selected)
        raise
    return {"status": "ok", "result": {**_describe(node), "creation_method": "class"}}


def _node(nuke, name):
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"Node not found: {name}")
    return node


def _same(a, b):
    return a is not None and b is not None and a.fullName() == b.fullName()


def _port(node, position, input_index=None):
    x, y = position
    width, height = node.screenWidth(), node.screenHeight()
    if node.Class() == "Dot":
        return [x + width // 2, y + height // 2]
    if input_index is None:
        return [x + width // 2, y + height]
    if node.Class() in ("Merge2", "Copy", "ShuffleCopy", "OFXcom.genarts.sapphire.distort.s_distort_v1") and input_index == 1:
        return [x, y + height // 2]
    if input_index > 0:
        if input_index != (2 if node.Class() == "Merge2" else 1):
            raise ValueError("Only primary, Merge A, and mask input ports are supported")
        if node.Class() != "Merge2" and not any(
            key in node.knobs() for key in ("maskChannelInput", "maskChannelMask")
        ):
            raise ValueError("Cannot infer a non-mask secondary input port")
        return [x + width, y + height // 2]
    return [x + width // 2, y]


def layout_nodes(params):
    import nuke

    placements, positions, routes, seen_inputs, corners = {}, [], [], set(), {}
    for item in params.get("positions", []):
        if set(item) != {"node_name", "x", "y"}:
            raise ValueError("Position requires exactly node_name, x and y")
        node = _node(nuke, item["node_name"])
        name = node.fullName()
        if name in placements:
            raise ValueError(f"Duplicate position: {name}")
        xy = [_integer(item["x"]), _integer(item["y"])]
        placements[name] = xy
        positions.append((node, xy, [node.xpos(), node.ypos()]))
    for item in params.get("routes", []):
        if (
            not {"source_node", "target_node"} <= set(item)
            or set(item) - {"source_node", "target_node", "input_index", "waypoints"}
        ):
            raise ValueError("Unknown route field")
        source = _node(nuke, item["source_node"])
        target = _node(nuke, item["target_node"])
        index = _integer(item.get("input_index", 0))
        if index < 0 or not _same(target.input(index), source):
            raise ValueError("Routing must preserve an existing source-to-target connection")
        if source.parent() != target.parent():
            raise ValueError("A route cannot cross Group boundaries")
        key = (target.fullName(), index)
        if key in seen_inputs:
            raise ValueError("Duplicate route for a target input")
        seen_inputs.add(key)
        points = []
        for point in item.get("waypoints", []):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("A waypoint must contain x and y")
            points.append([_integer(point[0]), _integer(point[1])])
        if len({tuple(point) for point in points}) != len(points):
            raise ValueError("A route cannot repeat a Dot center")
        predecessor = ("node", source.fullName())
        for point in points:
            key = tuple(point)
            signature = (source.fullName(), predecessor)
            if key in corners and corners[key] != signature:
                raise ValueError("Different image sources or route prefixes cannot overlap Dot centers")
            for old_point in corners:
                if old_point != key and abs(old_point[0] - key[0]) < 12 and abs(old_point[1] - key[1]) < 12:
                    raise ValueError("Dot rectangles overlap")
            for existing in source.parent().nodes():
                if existing.Class() != "Dot":
                    continue
                center = _port(existing, placements.get(existing.fullName(), [existing.xpos(), existing.ypos()]))
                if abs(center[0] - key[0]) < 12 and abs(center[1] - key[1]) < 12:
                    raise ValueError("Route overlaps an existing Dot")
            corners[key] = signature
            predecessor = ("dot", key)
        start = _port(source, placements.get(source.fullName(), [source.xpos(), source.ypos()]))
        end = _port(target, placements.get(target.fullName(), [target.xpos(), target.ypos()]), index)
        segments = list(zip([start, *points], [*points, end], strict=True))
        if any(a[0] != b[0] and a[1] != b[1] for a, b in segments):
            raise ValueError(f"Diagonal route segment: {source.fullName()} -> {target.fullName()}")
        routes.append((source, target, index, points, segments))
    result = {
        "dry_run": params.get("dry_run", True), "orthogonal": True,
        "positions": [{
            "node_name": node.fullName(), "x": xy[0], "y": xy[1],
            "width": node.screenWidth(), "height": node.screenHeight(),
            "output_port": _port(node, xy),
            "input_ports": {str(index): _port(node, xy, index) for index in (
                (0, 1, 2) if node.Class() == "Merge2" else (0, 1) if (
                    node.Class() in ("Copy", "ShuffleCopy", "OFXcom.genarts.sapphire.distort.s_distort_v1")
                    or node.Class() != "Dot" and any(key in node.knobs() for key in ("maskChannelInput", "maskChannelMask"))
                ) else (0,)
            )},
        } for node, xy, _ in positions],
        "routes": [{"source_node": source.fullName(), "target_node": target.fullName(),
                    "input_index": index, "segments": segments} for source, target, index, _, segments in routes],
        "created_dots": [],
    }
    if result["dry_run"]:
        return {"status": "ok", "result": result}
    created = []
    dot_cache = {}
    actual_routes = []
    try:
        for node, xy, _ in positions:
            node.setXYpos(*xy)
        for source, target, index, points, _ in routes:
            previous = source
            dots = []
            with source.parent():
                for x, y in points:
                    key = (source.fullName(), x, y)
                    dot = dot_cache.get(key)
                    if dot is None:
                        dot = nuke.nodes.Dot()
                        created.append(dot)
                        dot_cache[key] = dot
                        dot.setXYpos(x - dot.screenWidth() // 2, y - dot.screenHeight() // 2)
                        dot.setInput(0, previous)
                        if not _same(dot.input(0), previous):
                            raise RuntimeError("Nuke refused a Dot input connection")
                    elif not _same(dot.input(0), previous):
                        raise RuntimeError("Shared Dot has a different route predecessor")
                    dots.append(dot)
                    previous = dot
            target.setInput(index, previous)
            if not _same(target.input(index), previous):
                raise RuntimeError("Nuke refused the routed target connection")
            actual_points = [_port(source, [source.xpos(), source.ypos()])]
            actual_points.extend(_port(dot, [dot.xpos(), dot.ypos()]) for dot in dots)
            actual_points.append(_port(target, [target.xpos(), target.ypos()], index))
            segments = list(zip(actual_points[:-1], actual_points[1:], strict=True))
            if any(a[0] != b[0] and a[1] != b[1] for a, b in segments):
                raise RuntimeError("Nuke placement did not preserve planned orthogonal segments")
            actual_routes.append({"source_node": source.fullName(), "target_node": target.fullName(),
                                  "input_index": index, "segments": segments})
    except Exception as error:
        failures = []
        for source, target, index, _, _ in routes:
            try:
                target.setInput(index, source)
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        for node, _, old in positions:
            try:
                node.setXYpos(*old)
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        for dot in reversed(created):
            try:
                nuke.delete(dot)
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            raise RuntimeError(f"Layout failed: {error}; rollback also failed: {failures}") from error
        raise
    result["created_dots"] = [node.fullName() for node in created]
    result["routes"] = actual_routes
    for item, (node, _, _) in zip(result["positions"], positions, strict=True):
        item.update(x=node.xpos(), y=node.ypos())
    return {"status": "ok", "result": result}


def _check_callbacks(node):
    for key in ("onCreate", "onDestroy", "knobChanged", "updateUI", "autolabel"):
        if key in node.knobs() and node[key].value():
            raise ValueError(f"Cannot duplicate a node with a callback: {node.fullName()}.{key}")
    if node.Class() == "Group" or hasattr(node, "nodes"):
        for child in node.nodes():
            _check_callbacks(child)


def duplicate_node(params):
    import nuke

    source = _node(nuke, params["source_node"])
    position = params["position"]
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise ValueError("Position must contain x and y")
    xy = [_integer(value) for value in position]
    _check_callbacks(source)
    parent = source.parent()
    before = list(parent.nodes())
    selection = [(node, node.isSelected()) for node in before]
    try:
        with parent, tempfile.TemporaryDirectory(prefix="fx-nuke-copy-") as directory:
            path = str(Path(directory) / "node.nk")
            for node in before:
                node.setSelected(False)
            source.setSelected(True)
            nuke.nodeCopy(path)
            source.setSelected(False)
            nuke.nodePaste(path)
            copies = [node for node in parent.nodes() if node not in before]
            if len(copies) != 1:
                raise RuntimeError("Expected exactly one copied top-level node")
            clone = copies[0]
            for index in range(clone.inputs()):
                clone.setInput(index, None)
            clone.setXYpos(*xy)
            clone.setSelected(False)
    except Exception as error:
        failures = []
        for node in parent.nodes():
            if node not in before:
                try:
                    nuke.delete(node)
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
        if failures:
            raise RuntimeError(f"Copy failed: {error}; rollback also failed: {failures}") from error
        raise
    finally:
        for node, selected in selection:
            node.setSelected(selected)
    return {"status": "ok", "result": {
        "name": clone.fullName(), "class": clone.Class(), "xpos": clone.xpos(), "ypos": clone.ypos(),
        "inputs": [clone.input(i).fullName() if clone.input(i) else None for i in range(clone.inputs())],
    }}


def install(addon):
    addon.COMMANDS.update({"create_node": create_node, "import_nuke_toolset": import_toolset})
    addon.COMMANDS.update({"layout_nuke_nodes": layout_nodes, "duplicate_nuke_node": duplicate_node,
                           "get_nuke_graph_snapshot": graph_snapshot,
                           "get_selected_nodes": get_selected_nodes,
                           "collapse_nuke_dots": collapse_dots})


def collapse_dots(params):
    """Remove only explicitly named pass-through Dots, preserving downstream inputs."""
    import nuke

    dots = {_node(nuke, name).fullName(): _node(nuke, name) for name in params["dot_names"]}
    if not dots or any(node.Class() != "Dot" for node in dots.values()):
        raise ValueError("Specify only existing Dot nodes")
    parent = next(iter(dots.values())).parent()
    if any(node.parent() != parent for node in dots.values()):
        raise ValueError("Dot removal cannot cross Groups")

    def upstream(node):
        seen = set()
        while node is not None and node.fullName() in dots:
            if node.fullName() in seen:
                raise ValueError("Cyclic Dot chain")
            seen.add(node.fullName())
            node = node.input(0)
        return node

    changes = []
    for node in parent.nodes():
        if node.fullName() in dots:
            continue
        for index in range(node.inputs()):
            source = node.input(index)
            if source is not None and source.fullName() in dots:
                changes.append((node, index, source, upstream(source)))
    try:
        for node, index, _, source in changes:
            node.setInput(index, source)
            if node.input(index) is not None and not _same(node.input(index), source):
                raise RuntimeError("Nuke refused collapsed input")
            if source is not None and node.input(index) is None:
                raise RuntimeError("Nuke lost collapsed input")
    except Exception:
        for node, index, source, _ in changes:
            node.setInput(index, source)
        raise
    for dot in dots.values():
        nuke.delete(dot)
    return {"status": "ok", "result": {"removed_dots": list(dots)}}


def graph_snapshot(params):
    import nuke

    nodes = [_node(nuke, name) for name in params["node_names"]]
    if not nodes or any(node.parent() != nodes[0].parent() for node in nodes):
        raise ValueError("Tree layout requires nodes at one Group level")
    return {"status": "ok", "result": {"nodes": [{
        "name": node.fullName(), "class": node.Class(),
        "xpos": node.xpos(), "ypos": node.ypos(),
        "width": node.screenWidth(), "height": node.screenHeight(),
        "inputs": [node.input(i).fullName() if node.input(i) else None for i in range(node.inputs())],
    } for node in nodes]}}


def get_selected_nodes(params):
    """Read selection flags across the script without changing Group context."""
    import nuke

    root = nuke.root()
    selected = []

    def collect(group):
        for node in group.nodes():
            if node.isSelected():
                selected.append({
                    "name": node.fullName(), "class": node.Class(),
                    "group": group.fullName(), "xpos": node.xpos(), "ypos": node.ypos(),
                    "inputs": [node.input(i).fullName() if node.input(i) else None
                               for i in range(node.inputs())],
                })
            if hasattr(node, "nodes"):
                collect(node)

    collect(root)
    selected.sort(key=lambda node: node["name"])
    return {"status": "ok", "result": {
        "script": root.name(), "count": len(selected), "nodes": selected,
    }}
