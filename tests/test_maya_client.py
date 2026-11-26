"""Real loopback sockets, simulated scene dispatch; these are not Maya render tests."""

import json
import socket
import struct
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from fx_agent.maya import client
from fx_agent.maya.protocol import MAX_FRAME_BYTES, encode, receive


@pytest.fixture
def bridges(tmp_path):
    listeners, threads, states = [], [], []
    registry = tmp_path / "maya_sessions"
    registry.mkdir()

    def create(**changes):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)
        port = listener.getsockname()[1]
        state = {"software": "maya", "service": client.SERVICE, "protocol": 1,
                 "port": port, "pid": port, "session_id": str(port),
                 "scene_path": "D:/工程/灯光.ma", "maya_version": "2020",
                 "python_version": "2.7", "renderers": {}, "edits": 0,
                 "delay": 0, "running": True} | changes
        token = uuid.uuid4().hex + uuid.uuid4().hex
        (registry / f"{port}.json").write_text(json.dumps(state | {"token": token}), encoding="utf-8")
        states.append(state)
        listeners.append(listener)

        def serve():
            while state["running"]:
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with peer:
                    request = receive(peer)
                    result = dict(state)
                    error = ""
                    if request["token"] != token:
                        error = "authentication failed"
                    elif request["command"] != "identity":
                        if any(request["target"].get(k) != state[k] for k in (
                            "software", "service", "pid", "session_id", "port", "scene_path"
                        )):
                            error = "target changed"
                        elif request["command"] == "edit":
                            state["edits"] += 1
                        result = {"scene_path": state["scene_path"], "edits": state["edits"]}
                    time.sleep(state["delay"])
                    response = {"id": request["id"], "status": "error" if error else "success",
                                "result": result, "error": error}
                    with suppress(OSError):
                        peer.sendall(encode(response))

        thread = threading.Thread(target=serve, daemon=True)
        threads.append(thread)
        thread.start()
        return state

    yield create
    for state in states:
        state["running"] = False
    for listener in listeners:
        listener.close()
    for thread in threads:
        thread.join(timeout=1)


def select(connection, state):
    return connection.select(state["port"], state["pid"], state["session_id"], state["scene_path"])


def test_two_sessions_explicit_selection_isolated_per_client(bridges, tmp_path):
    first, second = bridges(), bridges()
    result = client.discover(tmp_path, (first["port"], second["port"]))
    assert len(result["sessions"]) == 2
    a, b = client.MayaConnection(tmp_path), client.MayaConnection(tmp_path)
    with pytest.raises(RuntimeError, match="Explicitly"):
        a.send("edit")
    select(a, first)
    select(b, second)
    a.send("edit")
    a.send("edit")
    b.send("edit")
    assert (first["edits"], second["edits"]) == (2, 1)
    assert b.info()["scene_path"] == second["scene_path"]


def test_stale_registry_wrong_software_and_failed_switch(bridges, tmp_path):
    good, wrong = bridges(), bridges(software="nuke")
    folder = tmp_path / "maya_sessions"
    (folder / "stale.json").write_text(json.dumps(good | {"session_id": "old"}), encoding="utf-8")
    (folder / "broken.json").write_text("broken", encoding="utf-8")
    result = client.discover(tmp_path, (wrong["port"],))
    assert [r["session_id"] for r in result["sessions"]] == [good["session_id"]]
    assert result["unavailable"][0]["port"] == wrong["port"]
    connection = client.MayaConnection(tmp_path)
    select(connection, good)
    old = connection.target
    with pytest.raises(ValueError, match="Not an FX Maya"):
        select(connection, wrong)
    assert connection.target == old
    with pytest.raises(ValueError, match="changed"):
        connection.select(good["port"], good["pid"], "old", good["scene_path"])
    assert connection.target == old


@pytest.mark.parametrize("changed", [{"pid": 4}, {"session_id": "replacement"}, {"scene_path": "other.ma"}])
def test_replacement_or_scene_change_never_receives_edit(bridges, changed, tmp_path):
    state = bridges()
    connection = client.MayaConnection(tmp_path)
    select(connection, state)
    state.update(changed)
    with pytest.raises(RuntimeError, match="target changed"):
        connection.send("edit")
    assert state["edits"] == 0


def test_lost_reply_is_not_retried_and_connection_is_poisoned(bridges, tmp_path):
    state = bridges()
    connection = client.MayaConnection(tmp_path)
    select(connection, state)
    state["delay"] = 0.15
    with pytest.raises(OSError):
        connection.send("edit", timeout=0.03)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        connection.send("edit")
    assert state["edits"] == 1


def test_pinned_connection_reports_failure_only_to_its_original_selection(bridges, tmp_path):
    first, second = bridges(), bridges()
    connection = client.MayaConnection(tmp_path)
    select(connection, first)
    pinned = connection.pinned()
    pinned.failed = True
    assert connection.failed
    select(connection, second)
    assert pinned.failed and not connection.failed
    pinned.failed = True
    assert not connection.failed


def test_protocol_unicode_partial_reads_limits_and_eof():
    class Peer:
        def __init__(self, data):
            self.data = data

        def recv(self, _size):
            value, self.data = self.data[:1], self.data[1:]
            return value

    value = {"path": "D:/中文/灯光.ma", "id": "test"}
    assert receive(Peer(encode(value))) == value
    with pytest.raises(IOError):
        receive(Peer(b"\x00"))
    with pytest.raises(ValueError, match="size"):
        receive(Peer(struct.pack("!I", MAX_FRAME_BYTES + 1)))
    with pytest.raises(ValueError):
        receive(Peer(encode({})[:4] + b"[]"))
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/fx_agent/maya/protocol.py").read_bytes() == (
        root / "integrations/maya/shared/fx_maya_bridge/protocol.py").read_bytes()


@pytest.mark.parametrize("port", [18186, 8187, 0, 65536, True])
def test_reserved_or_invalid_ports_rejected(port):
    with pytest.raises(ValueError):
        client.validate_port(port)
