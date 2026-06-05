"""
Enumerate 1..N-hop simple directed paths in the BKG, linearize each as text,
embed with OpenAI, and persist to Postgres `paths` table.

Paths are enumerated per `session_id` — graphs for different sessions are disjoint
in Neo4j, so we filter the node/edge load to a single session and never cross
session boundaries when walking. The `paths` table carries a `session_id` column
so multiple graphs can coexist in the same table.

Run:
    python compose_paths.py                       # auto-discover every session_id in nodes, enumerate paths for each
    python compose_paths.py --max-hops 2
    python compose_paths.py --session-id <sid>    # restrict to one session
    python compose_paths.py --dry-run --show 5    # preview path texts for every session, no writes
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras


def _pg_float_array(vals: list[float]) -> str:
    return "{" + ",".join(repr(float(v)) for v in vals) + "}"


def _pg_text_array(vals: list[str] | None) -> str:
    if not vals:
        return "{}"
    out = []
    for v in vals:
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        out.append(f'"{s}"')
    return "{" + ",".join(out) + "}"


def _copy_rows(cur, table: str, columns: list[str], rows) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    buf.seek(0)
    cur.copy_expert(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN "
        f"WITH (FORMAT csv, NULL '')",
        buf,
    )
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

PG_HOST = os.environ["PG_HOST"]
PG_PORT = os.environ["PG_PORT"]
PG_DATABASE = os.environ["PG_DATABASE"]
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

EMBED_MODEL = "text-embedding-3-small"
BATCH_SIZE = 128

_SCHEMA = "pwc_agent_utility_schema"

DDL = f"""
CREATE TABLE IF NOT EXISTS {_SCHEMA}.paths (
    path_id             SERIAL PRIMARY KEY,
    session_id          TEXT NOT NULL,
    hops                INT NOT NULL,
    node_element_ids    TEXT[] NOT NULL,
    node_labels         TEXT[] NOT NULL,
    relationship_types  TEXT[] NOT NULL,
    composed_text       TEXT NOT NULL,
    embedding           FLOAT8[] NOT NULL
);
ALTER TABLE {_SCHEMA}.paths ADD COLUMN IF NOT EXISTS session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_paths_session_id ON {_SCHEMA}.paths(session_id);
CREATE INDEX IF NOT EXISTS idx_paths_hops       ON {_SCHEMA}.paths(hops);
"""


def pg_connect():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
        user=PG_USER, password=PG_PASSWORD,
    )


def fetch_session_ids(conn) -> list[str]:
    """Return every distinct non-null session_id present in nodes, sorted."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT session_id FROM {_SCHEMA}.nodes "
            f"WHERE session_id IS NOT NULL ORDER BY session_id"
        )
        return [r[0] for r in cur.fetchall()]


def load_graph(conn, session_id: str) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str]]]:
    """Return (node_by_eid, unique_edges as (src, rel, tgt) tuples) scoped to one session."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT element_id, label, entity_type FROM {_SCHEMA}.nodes WHERE session_id = %s",
            (session_id,),
        )
        nodes = {r["element_id"]: dict(r) for r in cur.fetchall()}
        cur.execute(
            f"SELECT source_element_id, target_element_id, relationship_type "
            f"FROM {_SCHEMA}.edges WHERE session_id = %s",
            (session_id,),
        )
        raw = cur.fetchall()
    uniq: set[tuple[str, str, str]] = set()
    for e in raw:
        uniq.add((e["source_element_id"], e["relationship_type"], e["target_element_id"]))
    return nodes, sorted(uniq)


def enumerate_paths(
    nodes: dict[str, dict[str, Any]],
    edges: list[tuple[str, str, str]],
    max_hops: int,
) -> list[dict[str, Any]]:
    """DFS-enumerate simple directed paths of length 1..max_hops (no node repeats)."""
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for src, rel, tgt in edges:
        adj[src].append((rel, tgt))

    paths: list[dict[str, Any]] = []

    def dfs(current_nodes: list[str], current_rels: list[str]):
        if 1 <= len(current_rels) <= max_hops:
            paths.append(
                {
                    "hops": len(current_rels),
                    "node_eids": list(current_nodes),
                    "rel_types": list(current_rels),
                }
            )
        if len(current_rels) >= max_hops:
            return
        last = current_nodes[-1]
        for rel, tgt in adj.get(last, []):
            if tgt in current_nodes:  # simple path: no revisits
                continue
            current_nodes.append(tgt)
            current_rels.append(rel)
            dfs(current_nodes, current_rels)
            current_nodes.pop()
            current_rels.pop()

    for start in nodes:
        dfs([start], [])
    return paths


def linearize(path: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """Return (composed_text, node_labels) for a path."""
    labels = [nodes[eid]["label"] for eid in path["node_eids"]]
    parts = [labels[0]]
    for rel, lbl in zip(path["rel_types"], labels[1:]):
        parts.append(f"--[{rel}]-->")
        parts.append(lbl)
    text = "PATH: " + " ".join(parts)
    return text, labels


def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def write_paths(conn, session_id: str, rows: list[dict[str, Any]]) -> None:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL synchronous_commit = OFF")
        cur.execute(f"DELETE FROM {_SCHEMA}.paths WHERE session_id = %s", (session_id,))
        _copy_rows(
            cur,
            f"{_SCHEMA}.paths",
            ["session_id", "hops", "node_element_ids", "node_labels",
             "relationship_types", "composed_text", "embedding"],
            (
                (
                    session_id,
                    r["hops"],
                    _pg_text_array(r["node_eids"]),
                    _pg_text_array(r["node_labels"]),
                    _pg_text_array(r["rel_types"]),
                    r["composed_text"],
                    _pg_float_array(r["embedding"]),
                )
                for r in rows
            ),
        )
    conn.commit()


def process_session(
    conn,
    openai_client: OpenAI | None,
    session_id: str,
    *,
    max_hops: int,
    cap: int,
    show: int,
    dry_run: bool,
) -> None:
    nodes, edges = load_graph(conn, session_id)
    print(f"  loaded {len(nodes)} nodes, {len(edges)} unique edges for session_id={session_id}")

    if not nodes:
        print(f"  (nothing to enumerate for session_id={session_id}, skipping)\n")
        return

    print(f"  enumerating simple paths of length 1..{max_hops} ...")
    paths = enumerate_paths(nodes, edges, max_hops)
    by_hops: dict[int, int] = defaultdict(int)
    for p in paths:
        by_hops[p["hops"]] += 1
    print(f"  found {len(paths)} paths: " + ", ".join(f"{h}-hop={by_hops[h]}" for h in sorted(by_hops)))

    if cap and len(paths) > cap:
        paths = paths[:cap]
        print(f"  capped to {len(paths)}.")

    for p in paths:
        text, labels = linearize(p, nodes)
        p["composed_text"] = text
        p["node_labels"] = labels

    for p in paths[:show]:
        print(f"    [{p['hops']}h] {p['composed_text']}")

    if dry_run:
        print(f"  [dry-run] skipped embedding + write for session_id={session_id}.\n")
        return

    for i in range(0, len(paths), BATCH_SIZE):
        batch = paths[i : i + BATCH_SIZE]
        vectors = embed_batch(openai_client, [p["composed_text"] for p in batch])
        for p, v in zip(batch, vectors):
            p["embedding"] = v
        print(f"    embedded {min(i + BATCH_SIZE, len(paths))}/{len(paths)}")

    write_paths(conn, session_id, paths)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*), avg(array_length(embedding,1)) FROM {_SCHEMA}.paths WHERE session_id = %s",
            (session_id,),
        )
        n, dim = cur.fetchone()
    print(f"  wrote {n} paths ({int(dim)}-d embeddings) for session_id={session_id}.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--session-id",
        default=None,
        help="restrict to one session_id; omit to auto-discover and process every session found in nodes",
    )
    ap.add_argument("--max-hops", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=5)
    ap.add_argument("--cap", type=int, default=0, help="cap total paths per session (0=no cap)")
    args = ap.parse_args()

    conn = pg_connect()
    try:
        # DDL runs upfront so an ALTER ADD COLUMN happens before we read or filter on session_id.
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()

        all_sessions = fetch_session_ids(conn)
        if args.session_id:
            if args.session_id not in all_sessions:
                print(
                    f"WARNING: --session-id={args.session_id} not found in nodes "
                    f"(discovered: {all_sessions}). Proceeding anyway."
                )
            targets = [args.session_id]
        else:
            targets = all_sessions

        print(f"Postgres `{PG_DATABASE}` has {len(all_sessions)} distinct session_id(s) in nodes: {all_sessions}")
        print(f"Will process {len(targets)} session(s): {targets}\n")

        if not targets:
            print("Nothing to do. (Did you run compose_and_embed.py first?)")
            return 0

        openai_client: OpenAI | None = None if args.dry_run else OpenAI(api_key=OPENAI_API_KEY)

        for sid in targets:
            print(f"===== session_id={sid} =====")
            process_session(
                conn, openai_client, sid,
                max_hops=args.max_hops, cap=args.cap, show=args.show, dry_run=args.dry_run,
            )

        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {_SCHEMA}.paths")
            total = cur.fetchone()[0]
        print(f"Postgres `paths` total across all sessions: {total}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
