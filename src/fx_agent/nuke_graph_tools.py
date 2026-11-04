"""Explicit node-copy and orthogonal-layout operations, subject to production task gates."""


def register(mcp, connection):
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
