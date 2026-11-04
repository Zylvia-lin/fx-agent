"""Structured Viewer controls; the bridge uses Nuke APIs without desktop input."""


def register(mcp, connection):
    @mcp.tool(annotations={"readOnlyHint": True})
    def capture_nuke_interface(output_path: str | None = None) -> dict:
        """Capture only Nuke's main Qt widget to PNG for graph/layout review.

        QWidget.grab does not activate windows or capture other applications.
        Viewer OpenGL contents may differ; use capture_nuke_viewer for the image itself.
        """
        return connection.send_command("capture_nuke_interface", {"output_path": output_path})

    @mcp.tool()
    def frame_nuke_graph(node_names: list[str], zoom: float = 0.2) -> dict:
        """Frame named graph nodes using native nuke.zoom, without moving nodes or desktop focus."""
        return connection.send_command("frame_nuke_graph", {"node_names": node_names, "zoom": zoom})

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_nuke_viewer_state(viewer_name: str) -> dict:
        """Read a named Viewer's channels, inputs, frame, display settings and native F action."""
        return connection.send_command("get_nuke_viewer_state", {"viewer_name": viewer_name})

    @mcp.tool()
    def set_nuke_viewer_state(
        viewer_name: str, channels: str | None = None, exposure: float | None = None,
        gamma: float | None = None, frame: int | None = None,
        a_input: int | None = None, b_input: int | None = None,
        viewer_process: str | None = None,
        wipe: bool | None = None,
    ) -> dict:
        """Set Viewer-only controls and read back. Channels accepts a real layer or full channel.

        Exposure is in stops. wipe=False shows A alone without changing the B reference.
        Selection requires the named Viewer already be active
        inside Nuke; this tool never changes desktop focus or selects another window.
        """
        values = {"viewer_name": viewer_name, "channels": channels, "exposure": exposure,
                  "gamma": gamma, "frame": frame, "a_input": a_input, "b_input": b_input,
                  "viewer_process": viewer_process, "wipe": wipe}
        return connection.send_command("set_nuke_viewer_state", values)

    @mcp.tool()
    def connect_nuke_viewer(viewer_name: str, source_node: str | None, input_index: int = 0) -> dict:
        """Connect a source to Viewer input 0-9, or pass null to clear a comparison input."""
        return connection.send_command("connect_nuke_viewer", {
            "viewer_name": viewer_name, "source_node": source_node, "input_index": input_index,
        })

    @mcp.tool(annotations={"readOnlyHint": True})
    def capture_nuke_viewer(viewer_name: str, output_path: str | None = None) -> dict:
        """Capture the displayed Viewer image as JPEG using native Viewer.capture.

        Does not drive the desktop, move focus, change knobs, render or create nodes.
        May capture a mid-render image. Result includes file path, dimensions and actual
        Viewer settings; inspect the image before claiming visual completion.
        """
        return connection.send_command("capture_nuke_viewer", {
            "viewer_name": viewer_name, "output_path": output_path,
        })

    @mcp.tool()
    def fit_nuke_viewer(viewer_name: str) -> dict:
        """Invoke the named Viewer's native F menu action to fit/center the image.

        Requires an unambiguous F action and this Viewer already active inside Nuke.
        No OS keyboard, mouse, focus activation or shortcut simulation is used.
        """
        return connection.send_command("fit_nuke_viewer", {"viewer_name": viewer_name})
