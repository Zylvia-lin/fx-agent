# -*- coding: utf-8 -*-
"""Python 2/3 framing adapted from AutoMaya, MIT, (c) 2026 Adam Waters.

See integrations/maya/THIRD_PARTY.md and accompanying licenses in the source
checkout; the external Python package also carries maya/licenses and NOTICE.md.
"""
import json
import struct

MAX_FRAME_BYTES = 8 * 1024 * 1024


def text_value(value):
    """Preserve Maya's localized Unicode errors on Python 2 as well as Python 3."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        return type(u"")(value)
    except UnicodeError:
        fallback = repr(value)
        return fallback.decode("utf-8", "replace") if isinstance(fallback, bytes) else fallback


def encode(message):
    payload = json.dumps(message, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("Maya frame too large")
    return struct.pack("!I", len(payload)) + payload


def receive(sock):
    def exact(size):
        chunks = []
        while size:
            chunk = sock.recv(min(size, 65536))
            if not chunk:
                raise IOError("Maya socket closed before complete frame")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ValueError("Invalid Maya frame size")
    value = json.loads(exact(size).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Maya frame must be an object")
    return value
