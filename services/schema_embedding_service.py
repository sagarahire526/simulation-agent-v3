"""
Schema Embedding Service — Replaces full KG schema fetch with semantic search
over pre-embedded nodes and paths stored in PostgreSQL (nokia_embeddings DB).

Flow:
    1. Load node + path embedding indexes from PG (cached in-memory after first call)
    2. Embed the user query with OpenAI text-embedding-3-small
    3. Cosine-similarity search → combined top-K (nodes + paths, re-ranked)
    4. For every unique node label in the combined results, fetch its `props` JSONB
    5. Return formatted schema string: combined paths + node property details
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
DEFAULT_TOP_K = 5
MIN_SCORE = 0.0  # no floor — caller can override

# ── Module-level cache (thread-safe via lock) ────────────────────────────────
_lock = threading.Lock()
_node_rows: list[dict] | None = None
_node_mat: np.ndarray | None = None
_path_rows: list[dict] | None = None
_path_mat: np.ndarray | None = None


_PG_SCHEMA = "pwc_agent_utility_schema"


def _pg_emb_conn():
    """Connect to the nokia_syn_v1 PostgreSQL database."""
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=os.environ.get("PG_PORT", "5433"),
        dbname=os.environ.get("PG_DATABASE", "nokia_syn_v1"),
        user=os.environ.get("PG_USER", "postgres"),
        password=os.environ.get("PG_PASSWORD", "postgres"),
    )


def _load_indexes() -> tuple[list[dict], np.ndarray, list[dict], np.ndarray]:
    """Load and cache node + path embedding indexes from PostgreSQL."""
    global _node_rows, _node_mat, _path_rows, _path_mat

    with _lock:
        if _node_rows is not None and _path_rows is not None:
            return _node_rows, _node_mat, _path_rows, _path_mat

        conn = _pg_emb_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Nodes: element_id, node_id, label, entity_type, embedding
                cur.execute(
                    f"SELECT element_id, node_id, label, entity_type, embedding "
                    f"FROM {_PG_SCHEMA}.nodes ORDER BY label"
                )
                n_rows = [dict(r) for r in cur.fetchall()]

                # Paths: path_id, hops, node_labels, relationship_types, composed_text, embedding
                cur.execute(
                    f"SELECT path_id, hops, node_labels, relationship_types, "
                    f"composed_text, embedding FROM {_PG_SCHEMA}.paths"
                )
                p_rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        # Build and normalise embedding matrices
        n_mat = np.asarray([r["embedding"] for r in n_rows], dtype=np.float32)
        n_mat /= np.linalg.norm(n_mat, axis=1, keepdims=True)
        for r in n_rows:
            del r["embedding"]

        p_mat = np.asarray([r["embedding"] for r in p_rows], dtype=np.float32)
        p_mat /= np.linalg.norm(p_mat, axis=1, keepdims=True)
        for r in p_rows:
            del r["embedding"]

        _node_rows, _node_mat = n_rows, n_mat
        _path_rows, _path_mat = p_rows, p_mat

        logger.info(
            "Schema embedding indexes loaded: %d nodes, %d paths",
            len(n_rows), len(p_rows),
        )
        return _node_rows, _node_mat, _path_rows, _path_mat


def _embed_query(query: str) -> np.ndarray:
    """Embed a query string using OpenAI and return normalised vector."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    vec = np.asarray(
        client.embeddings.create(model=EMBED_MODEL, input=[query]).data[0].embedding,
        dtype=np.float32,
    )
    return vec / (np.linalg.norm(vec) or 1.0)


def _fetch_node_props(labels: set[str]) -> dict[str, dict[str, Any]]:
    """Fetch props JSONB from the nodes table for the given set of labels."""
    if not labels:
        return {}

    conn = _pg_emb_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT label, node_id, entity_type, props FROM {_PG_SCHEMA}.nodes "
                "WHERE label = ANY(%s)",
                (list(labels),),
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


def search_schema(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """
    Semantic search over embedded KG nodes and paths.

    Returns a formatted string suitable for injection into the traversal
    system prompt as {kg_schema}. Contains:
      1. Combined top-K paths (node+path, re-ranked by cosine similarity)
      2. Node property details for every unique node appearing in those paths

    Args:
        query:  The user's (refined) query.
        top_k:  Number of combined results to return.

    Returns:
        Formatted schema context string.
    """
    node_rows, n_mat, path_rows, p_mat = _load_indexes()
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

    # Fetch props for matched nodes
    node_props = _fetch_node_props(all_labels)

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
        "Schema embedding search: query=%s... → %d combined results, %d node details (%d chars)",
        query[:60], len(top_combined), len(node_props), len(schema_text),
    )
    print(
        f"\n  \033[92m🔍 Schema embedding search: {len(top_combined)} paths, "
        f"{len(node_props)} node details ({len(schema_text)} chars)\033[0m",
        flush=True,
    )

    return schema_text
