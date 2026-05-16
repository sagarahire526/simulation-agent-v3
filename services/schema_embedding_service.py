"""
Schema Embedding Service — Replaces full KG schema fetch with semantic search
over pre-embedded nodes and paths stored in PostgreSQL (nokia_embeddings DB).

Flow:
    1. Load node + path embedding indexes from PG for the requested session_id
       (cached in-memory per-session after first call)
    2. Embed the user query with OpenAI text-embedding-3-small
    3. Cosine-similarity search → combined top-K (nodes + paths, re-ranked)
    4. For every unique node label in the combined results, fetch its `props` JSONB
    5. Return formatted schema string: combined paths + node property details

Multiple disjoint BKG graphs live in the same Postgres tables, distinguished by
`session_id`. We resolve the caller's `project_type` (NTM / AHLOB / BOTH / NAS)
to a session_id and filter every read by it so NAS users never see telecom
embeddings and vice-versa.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

import numpy as np
import psycopg2
import psycopg2.extras
from openai import OpenAI

logger = logging.getLogger(__name__)

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536  # text-embedding-3-small output dim
DEFAULT_TOP_K = 5
MIN_SCORE = 0.0  # no floor — caller can override

_PG_SCHEMA = "pwc_agent_utility_schema"

# ── Project type → session_id mapping ────────────────────────────────────────
# Two disjoint BKG graphs share the same Postgres tables. project_type from the
# API maps to one of them so each user only ever sees their graph's embeddings.
SESSION_ID_DEFAULT = "69a3d22f26e208edc083a06e"  # NTM / AHLOB / Both — telecom graph
SESSION_ID_NAS     = "6a079b6bc1ea92432985ef54"  # NAS

_PROJECT_TYPE_TO_SESSION_ID: dict[str, str] = {
    "NTM":                      SESSION_ID_DEFAULT,
    "AHLOB Modernization":      SESSION_ID_DEFAULT,
    "NTM,AHLOB Modernization":  SESSION_ID_DEFAULT,
    "NAS":                      SESSION_ID_NAS,
}


def session_id_for_project(project_type: str) -> str:
    """Resolve a project_type string to its BKG session_id.

    Unknown / empty project_type falls back to the default (telecom) graph so
    existing callers keep working. A warning is logged so the mismatch is
    visible without breaking the request.
    """
    sid = _PROJECT_TYPE_TO_SESSION_ID.get(project_type)
    if sid is None:
        logger.warning(
            "Unknown project_type=%r — falling back to default session_id=%s",
            project_type, SESSION_ID_DEFAULT,
        )
        return SESSION_ID_DEFAULT
    return sid


# ── Module-level cache (thread-safe via lock), keyed by session_id ───────────
_lock = threading.Lock()
_indexes_by_session: dict[str, tuple[list[dict], np.ndarray, list[dict], np.ndarray]] = {}


def _pg_emb_conn():
    """Connect to the nokia_syn_v1 PostgreSQL database."""
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=os.environ.get("PG_PORT", "5433"),
        dbname=os.environ.get("PG_DATABASE", "nokia_syn_v1"),
        user=os.environ.get("PG_USER", "postgres"),
        password=os.environ.get("PG_PASSWORD", "postgres"),
    )


def _load_indexes(session_id: str) -> tuple[list[dict], np.ndarray, list[dict], np.ndarray]:
    """Load and cache node + path embedding indexes from PostgreSQL for one session.

    Each session_id gets its own cache entry, so the first request for a
    session pays the load cost and subsequent requests hit memory.
    """
    with _lock:
        cached = _indexes_by_session.get(session_id)
        if cached is not None:
            return cached

        conn = _pg_emb_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT element_id, node_id, label, entity_type, embedding "
                    f"FROM {_PG_SCHEMA}.nodes WHERE session_id = %s ORDER BY label",
                    (session_id,),
                )
                n_rows = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    f"SELECT path_id, hops, node_labels, relationship_types, "
                    f"composed_text, embedding FROM {_PG_SCHEMA}.paths "
                    f"WHERE session_id = %s",
                    (session_id,),
                )
                p_rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        if not n_rows and not p_rows:
            logger.warning(
                "Schema embedding indexes for session_id=%s are empty. "
                "Did you run compose_and_embed.py / compose_paths.py for this session?",
                session_id,
            )

        # Build and normalise embedding matrices. Empty session = empty matrices,
        # which `search_schema` then handles gracefully.
        if n_rows:
            n_mat = np.asarray([r["embedding"] for r in n_rows], dtype=np.float32)
            n_mat /= np.linalg.norm(n_mat, axis=1, keepdims=True)
            for r in n_rows:
                del r["embedding"]
        else:
            n_mat = np.zeros((0, EMBED_DIM), dtype=np.float32)

        if p_rows:
            p_mat = np.asarray([r["embedding"] for r in p_rows], dtype=np.float32)
            p_mat /= np.linalg.norm(p_mat, axis=1, keepdims=True)
            for r in p_rows:
                del r["embedding"]
        else:
            p_mat = np.zeros((0, EMBED_DIM), dtype=np.float32)

        _indexes_by_session[session_id] = (n_rows, n_mat, p_rows, p_mat)

        logger.info(
            "Schema embedding indexes loaded for session_id=%s: %d nodes, %d paths",
            session_id, len(n_rows), len(p_rows),
        )
        return _indexes_by_session[session_id]


def _embed_query(query: str) -> np.ndarray:
    """Embed a query string using OpenAI and return normalised vector."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    vec = np.asarray(
        client.embeddings.create(model=EMBED_MODEL, input=[query]).data[0].embedding,
        dtype=np.float32,
    )
    return vec / (np.linalg.norm(vec) or 1.0)


def _fetch_node_props(labels: set[str], session_id: str) -> dict[str, dict[str, Any]]:
    """Fetch props JSONB from the nodes table for the given set of labels, scoped to session_id."""
    if not labels:
        return {}

    conn = _pg_emb_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT label, node_id, entity_type, props FROM {_PG_SCHEMA}.nodes "
                "WHERE session_id = %s AND label = ANY(%s)",
                (session_id, list(labels)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    # Deduplicate by label (keep first)
    result: dict[str, dict[str, Any]] = {}
    for r in rows:
        lbl = r["label"]
        if lbl not in result:
            result[lbl] = {
                "node_id": r["node_id"],
                "entity_type": r["entity_type"],
                "props": r["props"],
            }
    return result


def _render_path(p: dict) -> str:
    """Strip 'PATH: ' prefix from composed_text."""
    return p["composed_text"].replace("PATH: ", "")


def _format_node_props(node_id: str, entity_type: str, props: dict) -> str:
    """Format a node's properties into a concise prompt-ready block."""
    lines = [f"  node_id: {node_id}"]
    if entity_type:
        lines.append(f"  type: {entity_type}")

    # Include the most useful properties for the traversal agent
    useful_keys = [
        "definition", "nl_description", "node_type",
        "kpi_description", "kpi_business_logic",
    ]
    for key in useful_keys:
        val = props.get(key)
        if val and str(val).strip():
            lines.append(f"  {key}: {str(val).strip()}")

    # Show filterable parameters from contracts
    for contract_key in ("kpi_contract", "map_contract"):
        contract = props.get(contract_key)
        if contract:
            import json as _json
            try:
                data = _json.loads(contract) if isinstance(contract, str) else contract
                params = [
                    p.get("name") for p in (data.get("parameters") or [])
                    if isinstance(p, dict) and p.get("name")
                ]
                if params:
                    lines.append(f"  filterable_by: {', '.join(params)}")
            except Exception:
                pass

    return "\n".join(lines)


def search_schema(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    project_type: str | None = None,
    session_id: str | None = None,
) -> str:
    """
    Semantic search over embedded KG nodes and paths, scoped to one session.

    Returns a formatted string suitable for injection into the traversal
    system prompt as {kg_schema}. Contains:
      1. Combined top-K paths (node+path, re-ranked by cosine similarity)
      2. Node property details for every unique node appearing in those paths

    Args:
        query:         The user's (refined) query.
        top_k:         Number of combined results to return.
        project_type:  API-layer project_type string ("NTM" / "AHLOB Modernization" /
                       "NTM,AHLOB Modernization" / "NAS"). Resolved to a session_id
                       via `session_id_for_project`. Ignored if `session_id` is given.
        session_id:    Explicit BKG session_id override. Takes precedence over
                       `project_type` — useful for tests / one-off scripts.

    Returns:
        Formatted schema context string. Empty schema text (with a stub header)
        if no rows exist for the resolved session_id.
    """
    if session_id is None:
        session_id = session_id_for_project(project_type or "")

    node_rows, n_mat, path_rows, p_mat = _load_indexes(session_id)
    if not node_rows and not path_rows:
        logger.warning(
            "search_schema: no embeddings available for session_id=%s (project_type=%r). "
            "Returning empty schema context.",
            session_id, project_type,
        )
        return f"── No KG embeddings indexed for session_id={session_id} ──"

    q_vec = _embed_query(query)

    # Cosine similarities
    n_scores = n_mat @ q_vec
    p_scores = p_mat @ q_vec

    n_idx = np.argsort(-n_scores)[:top_k]
    p_idx = np.argsort(-p_scores)[:top_k]

    # Combined: union of node + path top-K, re-ranked by score
    combined: dict[str, tuple[float, str, str]] = {}  # key → (score, display, type)
    for i in n_idx:
        lbl = node_rows[int(i)]["label"]
        etype = node_rows[int(i)].get("entity_type") or ""
        combined[f"NODE:{lbl}"] = (
            float(n_scores[i]),
            f"[NODE {etype}] {lbl}",
            "node",
        )
    for i in p_idx:
        p = path_rows[int(i)]
        key = f"PATH:{p['composed_text']}"
        combined[key] = (
            float(p_scores[i]),
            f"[PATH {p['hops']}h] {_render_path(p)}",
            "path",
        )

    top_combined = sorted(combined.values(), key=lambda t: -t[0])[:top_k]

    # Collect all unique node labels from the combined results
    all_labels: set[str] = set()
    for i in n_idx:
        lbl = node_rows[int(i)]["label"]
        if f"NODE:{lbl}" in combined:
            # Only include if this node made it into the top combined
            for score, display, _ in top_combined:
                if lbl in display:
                    all_labels.add(lbl)
                    break

    for i in p_idx:
        p = path_rows[int(i)]
        key = f"PATH:{p['composed_text']}"
        if key in combined:
            for score, display, _ in top_combined:
                if _render_path(p) in display:
                    # Add all node labels from this path
                    for lbl in (p.get("node_labels") or []):
                        all_labels.add(lbl)
                    break

    # Fetch props for matched nodes (scoped to the same session)
    node_props = _fetch_node_props(all_labels, session_id)

    # ── Format output ────────────────────────────────────────────────────────
    lines = ["── Relevant Graph Paths (ranked by semantic similarity) ──"]
    for score, display, _ in top_combined:
        lines.append(f"  {score:.4f}  {display}")

    if node_props:
        lines.append("")
        lines.append("── Node Details (properties for nodes in matched paths) ──")
        for lbl in sorted(node_props.keys()):
            info = node_props[lbl]
            lines.append(f"\n• {lbl} [{info['entity_type'] or '?'}]")
            lines.append(_format_node_props(info["node_id"], info["entity_type"], info["props"]))

    schema_text = "\n".join(lines)

    logger.info(
        "Schema embedding search [session_id=%s]: query=%s... → %d combined results, %d node details (%d chars)",
        session_id, query[:60], len(top_combined), len(node_props), len(schema_text),
    )
    print(
        f"\n  \033[92m🔍 Schema embedding search [session_id={session_id}]: "
        f"{len(top_combined)} paths, {len(node_props)} node details "
        f"({len(schema_text)} chars)\033[0m",
        flush=True,
    )

    return schema_text
