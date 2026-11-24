"""Optional lightweight local/remote embedding cache and hybrid similarity scoring.

Zero required external services: when FX_AGENT_EMBEDDING_URL is unset (and no custom
embedder is installed), every helper degrades silently to lexical-only retrieval.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import struct
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fx_agent.store import Store


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, f"Redirects disabled ({msg})", headers, fp)

LOG = logging.getLogger("fx_agent.embeddings")

_CUSTOM_EMBEDDER: Callable[[list[str]], list[list[float]]] | None = None
_CUSTOM_MODEL_NAME: str = "custom"
_QUERY_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_MAX_QUERY_CACHE = 128
_MAX_LAZY_BATCH = 64


def set_embedder(
    fn: Callable[[list[str]], list[list[float]]] | None,
    *,
    model: str = "custom",
) -> None:
    """Register or clear an in-process embedding callback (for local models or tests)."""
    global _CUSTOM_EMBEDDER, _CUSTOM_MODEL_NAME
    with _CACHE_LOCK:
        _CUSTOM_EMBEDDER = fn
        _CUSTOM_MODEL_NAME = model or "custom"
        _QUERY_CACHE.clear()


def embedding_model() -> str:
    if _CUSTOM_EMBEDDER is not None:
        return _CUSTOM_MODEL_NAME
    return os.getenv("FX_AGENT_EMBEDDING_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small"


def embedding_enabled() -> bool:
    return _CUSTOM_EMBEDDER is not None or bool(os.getenv("FX_AGENT_EMBEDDING_URL", "").strip())


def _normalize(vector: list[float]) -> list[float]:
    values = [float(x) for x in vector if math.isfinite(float(x))]
    if len(values) != len(vector) or not values:
        return []
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0.0:
        return []
    return [v / norm for v in values]


def encode_vector(vector: list[float]) -> str:
    packed = struct.pack(f"<{len(vector)}f", *vector)
    return base64.b64encode(packed).decode("ascii")


def decode_vector(encoded: str, dim: int) -> list[float]:
    raw = base64.b64decode(encoded.encode("ascii"))
    if len(raw) != dim * 4:
        return []
    return list(struct.unpack(f"<{dim}f", raw))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return max(0.0, min(1.0, dot))


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of strings; returns None on any provider error for graceful fallback."""
    if not texts or not embedding_enabled():
        return None
    cleaned = [t.strip()[:4000] or " " for t in texts]
    try:
        if _CUSTOM_EMBEDDER is not None:
            raw_vectors = _CUSTOM_EMBEDDER(cleaned)
        else:
            url = os.getenv("FX_AGENT_EMBEDDING_URL", "").strip()
            api_key = os.getenv("FX_AGENT_EMBEDDING_API_KEY", "").strip()
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
            data = json.dumps({"model": embedding_model(), "input": cleaned}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirectHandler()
            )
            with opener.open(req, timeout=8.0) as response:
                body = json.loads(response.read().decode("utf-8"))
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                ordered = sorted(
                    body["data"],
                    key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0,
                )
                raw_vectors = [item["embedding"] for item in ordered]
            elif isinstance(body, dict) and isinstance(body.get("embeddings"), list):
                raw_vectors = body["embeddings"]
            else:
                return None
        if not isinstance(raw_vectors, list) or len(raw_vectors) != len(cleaned):
            return None
        normalized = [_normalize(v) for v in raw_vectors]
        if any(not v for v in normalized):
            return None
        return normalized
    except Exception as exc:
        LOG.warning("Embedding provider failed; falling back to lexical search: %s", exc)
        return None


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cached_query_vector(query: str) -> list[float] | None:
    model = embedding_model()
    key = (model, query)
    with _CACHE_LOCK:
        cached = _QUERY_CACHE.get(key)
        if cached is not None:
            _QUERY_CACHE.move_to_end(key)
            return cached
    vectors = embed_texts([query])
    if not vectors:
        return None
    vec = vectors[0]
    with _CACHE_LOCK:
        _QUERY_CACHE[key] = vec
        while len(_QUERY_CACHE) > _MAX_QUERY_CACHE:
            _QUERY_CACHE.popitem(last=False)
    return vec


def semantic_scores(
    root: Path,
    *,
    item_type: str,
    software: str,
    query: str,
    items: dict[str, str],
) -> dict[str, float]:
    """Compute cosine similarity against cached SQLite/MySQL embeddings, lazily indexing misses."""
    if not embedding_enabled() or not query.strip() or not items:
        return {}
    query_vec = _cached_query_vector(query.strip())
    if not query_vec:
        return {}
    model = embedding_model()
    hashes = {item_id: _text_hash(text) for item_id, text in items.items()}
    cached_vecs: dict[str, list[float]] = {}
    missing_ids: list[str] = []
    try:
        with Store(root).connection() as db:
            rows = db.execute(
                "SELECT item_id,dim,text_hash,vector FROM embeddings WHERE item_type=? AND model=?",
                (item_type, model),
            ).fetchall()
            existing = {r[0]: (int(r[1]), r[2], r[3]) for r in rows}
            for item_id, digest in hashes.items():
                entry = existing.get(item_id)
                if entry and entry[1] == digest:
                    vec = decode_vector(entry[2], entry[0])
                    if len(vec) == len(query_vec):
                        cached_vecs[item_id] = vec
                        continue
                missing_ids.append(item_id)
        if missing_ids:
            batch_ids = missing_ids[:_MAX_LAZY_BATCH]
            vectors = embed_texts([items[i] for i in batch_ids])
            if vectors:
                now = datetime.now(UTC).isoformat()
                with Store(root).connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for item_id, vec in zip(batch_ids, vectors, strict=True):
                        if len(vec) != len(query_vec):
                            continue
                        cached_vecs[item_id] = vec
                        db.execute(
                            "INSERT INTO embeddings VALUES (?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(item_type,item_id,model) DO UPDATE SET "
                            "software=excluded.software,dim=excluded.dim,text_hash=excluded.text_hash,"
                            "vector=excluded.vector,updated_at=excluded.updated_at",
                            (
                                item_type,
                                item_id,
                                software or "all",
                                model,
                                len(vec),
                                hashes[item_id],
                                encode_vector(vec),
                                now,
                            ),
                        )
    except Exception as exc:
        LOG.warning("Embedding cache lookup failed; falling back to lexical search: %s", exc)
        return {}
    return {
        item_id: cosine_similarity(query_vec, vec)
        for item_id, vec in cached_vecs.items()
    }
