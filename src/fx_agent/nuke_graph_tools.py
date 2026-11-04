"""Explicit node-copy and orthogonal-layout operations, subject to production task gates."""


def register(mcp, connection):
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
