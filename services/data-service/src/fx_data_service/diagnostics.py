"""Correlated diagnostics without request bodies, credentials or validation input values."""

import logging
import os
import re
import traceback
import uuid
from pathlib import Path
from time import perf_counter

from pydantic import ValidationError

LOG = logging.getLogger("fx_data_service")


def configure_logging():
    level = os.getenv("FX_AGENT_LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Invalid FX_AGENT_LOG_LEVEL")
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    LOG.setLevel(level)
    return level.lower()


def error_details(exc):
    if not isinstance(exc, ValidationError):
        return []
    # Do not use Pydantic's str(exc), msg, input or ctx: they can contain secrets.
    from fx_agent.task_gates import SourceAdaptation, TaskSpec

    fields = set(TaskSpec.model_fields) | set(SourceAdaptation.model_fields) | {
        "request_id", "operation", "arguments", "task_id", "expected_revision", "method",
        "changes", "applies_when", "selected_sources", "source_adaptations", "no_match_reason",
        "max_chars", "offset", "limit", "module", "query", "spec", "files", "path", "role",
        "name", "default", "source", "destination", "study_id", "case_id", "detail",
    }
    return [{"field": [part if isinstance(part, int) or part in fields else "<unknown>"
                        for part in error["loc"][:8]], "type": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)[:20]]


def safe_reason(exc):
    message = str(exc) if not isinstance(exc, ValidationError) else ""
    fixed = {
        "Task revision conflict; read fx_get_task",
        "Selected knowledge must have been read in full for this task",
        "Explain why retrieved knowledge does not apply",
        "Each selected source requires one source adaptation",
        "Task plan exceeds its evidence/text budget",
        "Task endpoint busy; wait for its running operation",
        "Plan needs method, applicability and adaptation",
        "Task is not ready; understand requirements, read knowledge and save a plan",
        "Client update required for verified export downloads",
        "Template study checksum validation failed",
        "File path is outside the service's allowed shared roots; configure a path mapping",
    }
    if message in fixed:
        return message
    if re.fullmatch(r"Missing actual knowledge reads: [a-zA-Z0-9_:,]+", message):
        return message
    return "See error type, validation fields and frame locations"


def log_rejection(request, exc, code):
    state = request.state
    # Frame locations still identify the failing code when the exception message
    # itself may contain request values. No locals or source lines are emitted.
    frames = [f"{Path(f.filename).name}:{f.lineno}:{f.name}"
              for f in traceback.extract_tb(exc.__traceback__)]
    LOG.warning("operation_rejected request_id=%s operation=%s project=%s device=%s "
                "error_code=%s error_type=%s reason=%s details=%s frames=%s",
                state.request_id, getattr(state, "operation", "unknown"),
                getattr(state, "data_project", "unknown"),
                getattr(state, "log_device", "unknown"), code,
                type(exc).__name__, safe_reason(exc), error_details(exc), frames)


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        state["request_id"] = uuid.uuid4().hex
        started, status = perf_counter(), 500

        async def response(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []),
                                      (b"x-fx-request-id", state["request_id"].encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, response)
        finally:
            level = logging.DEBUG if scope["path"] == "/internal/health" else logging.INFO
            if status >= 400:
                level = logging.WARNING
            LOG.log(level, "request_complete request_id=%s method=%s path=%r status=%s "
                    "duration_ms=%.1f operation=%s project=%s device=%s",
                    state["request_id"], scope["method"], scope["path"][:512], status,
                    (perf_counter() - started) * 1000, state.get("operation", "-"),
                    state.get("data_project", "-"), state.get("log_device", "-"))
