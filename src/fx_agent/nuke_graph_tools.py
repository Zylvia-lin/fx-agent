"""Explicit node-copy and orthogonal-layout operations, subject to production task gates."""


def register(mcp, connection):
    @mcp.tool()
    def create_node(node_class: str, name: str | None = None, knobs: dict | None = None,
                    position: list[int] | None = None, toolset_path: str | None = None) -> dict:
        """Create a native/plugin class first; if unknown, import a same-name .nk toolset.

        Searches Nuke plugin paths and ~/.nuke/ToolSets recursively. Ambiguous matches
        require toolset_path. Fallback requires exactly one top-level node/Group.
        Parameter/license errors do not trigger fallback. Returns the actual class/name.
        """
        params = {
            "node_class": node_class, "name": name, "knobs": knobs or {},
            "position": position, "toolset_path": toolset_path,
        }
        return connection.send_command("create_node", {k: v for k, v in params.items() if v is not None})

    @mcp.tool()
    def import_nuke_toolset(path: str, name: str | None = None, knobs: dict | None = None,
                            position: list[int] | None = None) -> dict:
        """Paste an absolute .nk toolset path into the current Group without replacing the script.

        Returns all imported top-level nodes. name/knobs require a single node;
        position translates the imported block while preserving internal placement.
        Restores existing selection and removes imported nodes on failure.
        """
        return connection.send_command("import_nuke_toolset", {
            "path": path, "name": name, "knobs": knobs or {}, "position": position,
        })

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_selected_nodes() -> dict:
        """Read all selected node flags in the current script, including inside Groups.

        Returns script, count and nodes with full names, classes, parent Groups,
        positions and indexed input names. Includes selections retained in other
        Group graphs; does not infer active-panel focus or change selection.
        No selection returns an empty list. Use this when users point to selected nodes.
        """
        return connection.send_command("get_selected_nodes")

    @mcp.tool()
    def collapse_nuke_dots(dot_names: list[str]) -> dict:
        """Remove explicitly named pass-through Dots while preserving downstream image inputs.

        For replacing routing made by this task; never removes processing nodes.
        """
        return connection.send_command("collapse_nuke_dots", {"dot_names": dot_names})

    @mcp.tool()
    def layout_nuke_blocks(blocks: list[dict], dry_run: bool = True) -> dict:
        """Translate explicit effect blocks without changing their internal layout.

        Each block: {node_names,x,y}. Groups stay intact. Changes only positions;
        no image knobs, image inputs, or automatic obstacle routing. Blocks must not overlap.
        Use layout_nuke_nodes for a separately reviewed bus and a few shared-reference Dots.
        """
        names = [name for block in blocks for name in block["node_names"]]
        if len(names) != len(set(names)):
            raise ValueError("Effect blocks cannot share processing nodes")
        snapshot = connection.send_command("get_nuke_graph_snapshot", {"node_names": names})
        if snapshot.get("status") == "error" or "nodes" not in snapshot:
            return snapshot
        nodes = {node["name"]: node for node in snapshot["nodes"]}
        positions, bounds = [], []
        for block in blocks:
            if type(block["x"]) is not int or type(block["y"]) is not int:
                raise ValueError("Block origins require integer coordinates")
            if not block["node_names"]:
                raise ValueError("An effect block must contain nodes")
            left = min(nodes[name]["xpos"] for name in block["node_names"])
            top = min(nodes[name]["ypos"] for name in block["node_names"])
            placed = [{"node_name": name, "x": nodes[name]["xpos"] - left + block["x"],
                       "y": nodes[name]["ypos"] - top + block["y"]} for name in block["node_names"]]
            right = max(p["x"] + nodes[p["node_name"]]["width"] for p in placed)
            bottom = max(p["y"] + nodes[p["node_name"]]["height"] for p in placed)
            rect = (block["x"], block["y"], right, bottom)
            if any(rect[0] < c and rect[2] > a and rect[1] < d and rect[3] > b for a, b, c, d in bounds):
                raise ValueError("Effect blocks overlap; no nodes moved")
            bounds.append(rect)
            positions.extend(placed)
        return connection.send_command("layout_nuke_nodes", {
            "positions": positions, "routes": [], "dry_run": dry_run,
        })

    @mcp.tool()
    def layout_nuke_nodes(positions: list[dict], routes: list[dict], dry_run: bool = True) -> dict:
        """Position only named nodes and route existing connections through explicit Dot centers.

        Positions: {node_name,x,y}; routes: {source_node,target_node,input_index,waypoints:[[x,y],...]}.
        Dry-run validates all coordinates, existing wiring, and horizontal/vertical segments.
        Execution adds native Dot nodes and preserves connection semantics. No global auto-layout.
        """
        return connection.send_command("layout_nuke_nodes", {
            "positions": positions, "routes": routes, "dry_run": dry_run,
        })

    @mcp.tool()
    def duplicate_nuke_node(source_node: str, position: list[int]) -> dict:
        """Copy one existing node/Group via a temporary file without using the clipboard.

        Rejects node callbacks recursively, restores existing selection, disconnects the new
        node from old inputs and places only the new copy. Does not rename original nodes.
        """
        return connection.send_command("duplicate_nuke_node", {
            "source_node": source_node, "position": position,
        })
