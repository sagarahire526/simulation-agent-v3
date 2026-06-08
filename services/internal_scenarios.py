"""
Internal Scenario Library — local JSON-backed semantic search over program-office
simulation scenarios that have not yet been ingested into the GCL semantic layer.

Storage:
    data/internal_scenarios.json — {version, embedding_model, scenarios: [...]}
    Each scenario: {id, tag, question, steps[], embedding[], embedding_model, created_at}

Embedding model: text-embedding-3-small (matches scripts/compose_and_embed.py).
The query is embedded at runtime; cosine similarity is computed against each stored
question vector. Top-K matches above MIN_SIMILARITY are returned.

The service is loaded lazily (module-level singleton) and reloads the JSON file
when its mtime changes — so a write via the API endpoint becomes visible to the
next planner call without a restart.

Failure modes:
  • File missing or unreadable → empty library, search returns [].
  • OpenAI embeddings call fails → search returns []; planner falls through to Mode B.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

import config

logger = logging.getLogger(__name__)

# ── Module config ────────────────────────────────────────────────────────────
EMBEDDING_MODEL = "text-embedding-3-small"
MIN_SIMILARITY = 0.8
DEFAULT_TOP_K = 1

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_FILE = _PROJECT_ROOT / "data" / "internal_scenarios.json"


# ── In-memory cache (reloaded on file mtime change) ──────────────────────────
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "mtime": 0.0,
    "scenarios": [],   # list of full entries with embeddings
    "version": 1,
    "embedding_model": EMBEDDING_MODEL,
}

# OpenAI client is created lazily; the gateway-vs-direct decision comes from env
_openai_client: OpenAI | None = None


def _get_openai_client() -> OpenAI:
    """Create (or reuse) the OpenAI client for embedding calls."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY not set — cannot embed queries for the internal scenario library."
        )
    _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


def _embed(text: str) -> list[float]:
    """Embed a single text using text-embedding-3-small. Raises on failure."""
    client = _get_openai_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ── File I/O ─────────────────────────────────────────────────────────────────

def _load_from_disk() -> dict[str, Any]:
    """Read the JSON file. Returns the default empty shape on any error."""
    if not _DATA_FILE.is_file():
        return {"version": 1, "embedding_model": EMBEDDING_MODEL, "scenarios": []}
    try:
        with _DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("scenarios"), list):
            data["scenarios"] = []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Internal scenarios file unreadable (%s): %s", _DATA_FILE, exc)
        return {"version": 1, "embedding_model": EMBEDDING_MODEL, "scenarios": []}


def _ensure_loaded() -> None:
    """Reload from disk if the file's mtime changed since last load."""
    try:
        mtime = _DATA_FILE.stat().st_mtime if _DATA_FILE.is_file() else 0.0
    except OSError:
        mtime = 0.0
    with _cache_lock:
        if mtime != _cache["mtime"]:
            data = _load_from_disk()
            _cache["mtime"] = mtime
            _cache["scenarios"] = data.get("scenarios", [])
            _cache["version"] = data.get("version", 1)
            _cache["embedding_model"] = data.get("embedding_model", EMBEDDING_MODEL)
            logger.info(
                "Loaded internal scenario library: %d entries (model=%s)",
                len(_cache["scenarios"]), _cache["embedding_model"],
            )


def _atomic_write(data: dict[str, Any]) -> None:
    """Write the JSON file atomically (.tmp → rename)."""
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _DATA_FILE)


# ── Public API ───────────────────────────────────────────────────────────────

def search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = MIN_SIMILARITY,
) -> list[dict[str, Any]]:
    """
    Semantic search over stored scenarios.

    Returns a list of {id, tag, question, steps, similarity_score} sorted by
    similarity desc, filtered by the threshold. Returns [] if the library is
    empty, the embedding call fails, or no scenarios clear the threshold.
    """
    _ensure_loaded()
    scenarios = _cache["scenarios"]
    if not scenarios or not query.strip():
        return []

    try:
        query_vec = _embed(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Internal scenario embedding failed (non-fatal): %s", exc)
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in scenarios:
        emb = entry.get("embedding") or []
        score = _cosine(query_vec, emb)
        if score >= min_similarity:
            scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, entry in scored[:top_k]:
        out.append({
            "id":               entry.get("id"),
            "tag":              entry.get("tag"),
            "question":         entry.get("question"),
            "steps":            entry.get("steps", []),
            "similarity_score": round(float(score), 4),
        })
    return out


def format_for_planner(matches: list[dict[str, Any]]) -> str:
    """
    Format matches as a markdown block the planner can read alongside the GCL
    semantic context. Mirrors the shape of SemanticService's "Matched Simulation
    Scenarios" section so Mode A logic in the planner prompt handles it
    uniformly.
    """
    if not matches:
        return ""
    lines: list[str] = [
        "### Matched Internal Scenarios (Program Office Library)",
        (
            "These are vetted simulation scenarios from the program office, not yet "
            "ingested into the GCL semantic layer. When a match here scores higher "
            "than the GCL Matched Simulation Scenario above, prefer this one as your "
            "Mode A skeleton. Steps are mixed retrieval + synthesis — apply Rule 1 "
            "to emit only retrieval steps."
        ),
        "",
    ]
    for m in matches:
        score = f"{m['similarity_score'] * 100:.1f}%"
        lines.append(f"**Internal {m.get('id', '?')} — {m.get('tag', '')}** (similarity: {score})")
        if m.get("question"):
            lines.append(f"  *Question:* \"{m['question']}\"")
        steps = m.get("steps", [])
        if steps:
            lines.append("  *Steps to solve (apply Rule 1 — emit only retrieval steps):*")
            for i, s in enumerate(steps, 1):
                lines.append(f"    {i}. {s}")
        lines.append("")
    return "\n".join(lines)


def list_all(include_embeddings: bool = False) -> list[dict[str, Any]]:
    """Return all stored scenarios. Embeddings stripped by default to keep payloads small."""
    _ensure_loaded()
    out = []
    for entry in _cache["scenarios"]:
        item = {k: v for k, v in entry.items() if include_embeddings or k != "embedding"}
        out.append(item)
    return out


def add(tag: str, question: str, steps: list[str]) -> dict[str, Any]:
    """
    Embed the question and append a new scenario to the library. Persists
    atomically to disk and refreshes the in-memory cache.

    Returns the saved entry (without the embedding vector).
    """
    if not tag or not question or not isinstance(steps, list) or not steps:
        raise ValueError("tag, question, and a non-empty steps list are required.")

    embedding = _embed(question)
    entry = {
        "id":              f"S{uuid.uuid4().hex[:8]}",
        "tag":             tag.strip(),
        "question":        question.strip(),
        "steps":           [str(s).strip() for s in steps if str(s).strip()],
        "embedding":       embedding,
        "embedding_model": EMBEDDING_MODEL,
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }

    with _cache_lock:
        data = _load_from_disk()
        data.setdefault("scenarios", []).append(entry)
        data["embedding_model"] = EMBEDDING_MODEL
        _atomic_write(data)
        # Force reload on next access
        _cache["mtime"] = 0.0

    return {k: v for k, v in entry.items() if k != "embedding"}


def delete(scenario_id: str) -> bool:
    """Remove a scenario by id. Returns True if removed, False if not found."""
    with _cache_lock:
        data = _load_from_disk()
        before = len(data.get("scenarios", []))
        data["scenarios"] = [s for s in data.get("scenarios", []) if s.get("id") != scenario_id]
        if len(data["scenarios"]) == before:
            return False
        _atomic_write(data)
        _cache["mtime"] = 0.0
    return True
