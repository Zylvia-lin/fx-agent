"""Shared data execution context; independent of network and DCC clients."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from pydantic import BaseModel

_scope = ContextVar("fx_data_service_scope", default=None)


@contextmanager
def service_scope(data: Path, database: Path, device_id: str, backend=None):
    """Propagates to asyncio worker threads without changing process environment."""
    token = _scope.set((Path(data), Path(database), device_id, backend))
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope():
    return _scope.get()


def current_device() -> str:
    scope = current_scope()
    return scope[2] if scope else ""


def current_backend(root=None):
    scope = current_scope()
    if scope and (root is None or Path(root).resolve() == scope[0].resolve()):
        return scope[3]
    return None


def remote_enabled() -> bool:
    if current_scope():
        return False
    mode = os.getenv("FX_AGENT_DATA_MODE", "auto")
    if mode not in {"auto", "local", "service"}:
        raise ValueError("FX_AGENT_DATA_MODE must be local or service")
    return (
        mode == "service"
        or mode == "auto"
        and bool(
            os.getenv("FX_AGENT_SERVICE_URL")
            or os.getenv("FX_AGENT_CLIENT_CONFIG")
            or os.getenv("FX_AGENT_SERVER_IP")
        )
    )


def json_value(value):
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode="json"))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value
