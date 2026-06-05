"""
Fetch BKGNodes + edges from Neo4j, compose a relevance-optimized text per node,
embed with OpenAI, and persist everything to Postgres (nokia_embeddings DB).

Tables created:
    nodes(node_id PK, label, entity_type, node_type, composed_text, embedding float8[], props jsonb)
    edges(edge_id PK, source_id FK, target_id FK, relationship_type)

Run:
    python compose_and_embed.py                       # auto-discover every session_id in Neo4j and load each
    python compose_and_embed.py --dry-run             # preview composed text for every session, no writes
    python compose_and_embed.py --session-id <sid>    # restrict to one session
    python compose_and_embed.py --limit 3             # cap at first 3 nodes per session (smoke test)

Each BKGNode in Neo4j carries a `session_id` property identifying which graph it
belongs to. Multiple disjoint graphs can live in the same Neo4j DB; we mirror that
in Postgres via a `session_id` column on `nodes`/`edges` so each load only touches
rows for the given session. Without --session-id the script enumerates every
distinct session_id present on BKGNodes and processes them sequentially.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras


def _pg_float_array(vals: list[float]) -> str:
    """Format a Python list of floats as a PostgreSQL array literal."""
    return "{" + ",".join(repr(float(v)) for v in vals) + "}"


def _pg_text_array(vals: list[str] | None) -> str:
    """Format a Python list of strings as a PostgreSQL text[] array literal."""
    if not vals:
        return "{}"
    out = []
    for v in vals:
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        out.append(f'"{s}"')
    return "{" + ",".join(out) + "}"


def _copy_rows(cur, table: str, columns: list[str], rows) -> None:
    """Stream rows into Postgres via COPY ... FROM STDIN (CSV).

    `rows` is an iterable of tuples whose values are already serialized to
    strings appropriate for each column type (PG array literals, JSON text,
    plain text). None values become CSV empty fields → SQL NULL.
    """
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
from neo4j import GraphDatabase
from openai import OpenAI

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USER = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]
NEO4J_DATABASE = os.environ["NEO4J_DATABASE"]

PG_HOST = os.environ["PG_HOST"]
PG_PORT = os.environ["PG_PORT"]
PG_DATABASE = os.environ["PG_DATABASE"]
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
BATCH_SIZE = 64

_SCHEMA = "pwc_agent_utility_schema"

DDL = f"""

CREATE TABLE IF NOT EXISTS {_SCHEMA}.nodes (
    element_id    TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    label         TEXT NOT NULL,
    entity_type   TEXT,
    node_type     TEXT,
    composed_text TEXT NOT NULL,
    embedding     FLOAT8[] NOT NULL,
    props         JSONB NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE {_SCHEMA}.nodes ADD COLUMN IF NOT EXISTS session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_nodes_session_id  ON {_SCHEMA}.nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_nodes_node_id     ON {_SCHEMA}.nodes(node_id);
CREATE INDEX IF NOT EXISTS idx_nodes_label       ON {_SCHEMA}.nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_entity_type ON {_SCHEMA}.nodes(entity_type);

CREATE TABLE IF NOT EXISTS {_SCHEMA}.edges (
    edge_id           TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    source_element_id TEXT NOT NULL REFERENCES {_SCHEMA}.nodes(element_id) ON DELETE CASCADE,
    target_element_id TEXT NOT NULL REFERENCES {_SCHEMA}.nodes(element_id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL
);
ALTER TABLE {_SCHEMA}.edges ADD COLUMN IF NOT EXISTS session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_edges_session_id ON {_SCHEMA}.edges(session_id);
CREATE INDEX IF NOT EXISTS idx_edges_source  ON {_SCHEMA}.edges(source_element_id);
CREATE INDEX IF NOT EXISTS idx_edges_target  ON {_SCHEMA}.edges(target_element_id);
CREATE INDEX IF NOT EXISTS idx_edges_reltype ON {_SCHEMA}.edges(relationship_type);
"""


def fetch_session_ids(session) -> list[str]:
    """Return every distinct non-null session_id present on BKGNodes, sorted."""
    cypher = """
    MATCH (n:BKGNode)
    WHERE n.session_id IS NOT NULL
    RETURN DISTINCT n.session_id AS session_id
    ORDER BY session_id
    """
    return [r["session_id"] for r in session.run(cypher)]


def fetch_nodes_with_neighbors(session, session_id: str) -> list[dict[str, Any]]:
    cypher = """
    MATCH (n:BKGNode {session_id: $session_id})
    OPTIONAL MATCH (n)-[r_out:RELATES_TO]->(m_out:BKGNode {session_id: $session_id})
    WITH n,
         collect(DISTINCT CASE WHEN m_out IS NULL THEN NULL ELSE
            {label: m_out.label, rel: r_out.relationship_type}
         END) AS out_edges
    OPTIONAL MATCH (n)<-[r_in:RELATES_TO]-(m_in:BKGNode {session_id: $session_id})
    WITH n, out_edges,
         collect(DISTINCT CASE WHEN m_in IS NULL THEN NULL ELSE
            {label: m_in.label, rel: r_in.relationship_type}
         END) AS in_edges
    RETURN elementId(n) AS element_id,
           n.node_id AS node_id,
           properties(n) AS props,
           [e IN out_edges WHERE e IS NOT NULL] AS out_edges,
           [e IN in_edges  WHERE e IS NOT NULL] AS in_edges
    ORDER BY n.label
    """
    return [dict(r) for r in session.run(cypher, session_id=session_id)]


def fetch_edges(session, session_id: str) -> list[dict[str, Any]]:
    cypher = """
    MATCH (a:BKGNode {session_id: $session_id})-[r:RELATES_TO]->(b:BKGNode {session_id: $session_id})
    RETURN toString(elementId(r)) AS edge_id,
           elementId(a) AS source_element_id,
           elementId(b) AS target_element_id,
           r.relationship_type AS relationship_type
    """
    return [dict(r) for r in session.run(cypher, session_id=session_id)]


def _contract_params(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    params = data.get("parameters") or []
    return [p.get("name") for p in params if isinstance(p, dict) and p.get("name")]


def compose_text(row: dict[str, Any]) -> str:
    p = row["props"]
    label = p.get("label") or ""
    entity_type = p.get("entity_type") or ""
    node_type = p.get("node_type") or ""
    definition = (p.get("definition") or p.get("nl_description") or "").strip()
    business_rule = (p.get("nl_description") or "").strip()
    params = _contract_params(p.get("map_contract") or p.get("kpi_contract")) 

    out_edges = row.get("out_edges") or []
    in_edges = row.get("in_edges") or []

    lines: list[str] = []
    lines.append(f"ENTITY: {label}")
    if entity_type or node_type:
        lines.append(f"TYPE: {entity_type} / {node_type}".strip(" /"))
    if definition:
        lines.append(f"DEFINITION: {definition}")
    if business_rule:
        lines.append(f"BUSINESS_RULE: {business_rule}")
    if params:
        lines.append(f"FILTERABLE_BY: {', '.join(params)}")
    if out_edges:
        outs = sorted({f"{e['label']} ({e['rel']})" for e in out_edges if e.get("label") and e.get("rel")})
        if outs:
            lines.append(f"CONNECTS_TO: {', '.join(outs)}")
    if in_edges:
        ins = sorted({f"{e['label']} ({e['rel']})" for e in in_edges if e.get("label") and e.get("rel")})
        if ins:
            lines.append(f"REFERENCED_BY: {', '.join(ins)}")

    return "\n".join(lines)


def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def pg_connect():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
        user=PG_USER, password=PG_PASSWORD,
    )


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def write_nodes(conn, session_id: str, rows: list[dict[str, Any]]) -> None:
    with conn.cursor() as cur:
        # Loosen durability for the duration of this transaction — bulk loads
        # don't need fsync per commit.
        cur.execute("SET LOCAL synchronous_commit = OFF")
        # Only wipe rows for this session — sibling graphs in the same table stay intact.
        cur.execute(f"DELETE FROM {_SCHEMA}.nodes WHERE session_id = %s", (session_id,))
        _copy_rows(
            cur,
            f"{_SCHEMA}.nodes",
            ["element_id", "session_id", "node_id", "label", "entity_type",
             "node_type", "composed_text", "embedding", "props"],
            (
                (
                    r["element_id"],
                    session_id,
                    r["node_id"],
                    r["label"],
                    r["entity_type"],
                    r["node_type"],
                    r["composed_text"],
                    _pg_float_array(r["embedding"]),
                    json.dumps(r["props"]),
                )
                for r in rows
            ),
        )
    conn.commit()


def write_edges(conn, session_id: str, edges: list[dict[str, Any]]) -> None:
    # Dedupe by edge_id to avoid CardinalityViolation if any duplicates slipped in.
    seen: dict[str, dict[str, Any]] = {}
    for e in edges:
        seen.setdefault(e["edge_id"], e)
    deduped = list(seen.values())
    with conn.cursor() as cur:
        cur.execute("SET LOCAL synchronous_commit = OFF")
        # Node-side DELETE above cascades to edges, but be explicit in case session_id
        # has stale edges left over from a partial earlier run.
        cur.execute(f"DELETE FROM {_SCHEMA}.edges WHERE session_id = %s", (session_id,))
        _copy_rows(
            cur,
            f"{_SCHEMA}.edges",
            ["edge_id", "session_id", "source_element_id",
             "target_element_id", "relationship_type"],
            (
                (e["edge_id"], session_id, e["source_element_id"],
                 e["target_element_id"], e["relationship_type"])
                for e in deduped
            ),
        )
    conn.commit()


def process_session(
    neo4j_session,
    pg_conn,
    openai_client: OpenAI | None,
    session_id: str,
    *,
    limit: int,
    show: int,
    dry_run: bool,
) -> None:
    """Fetch + embed + write a single session_id's graph. pg_conn/openai_client may be None on dry-run."""
    node_rows = fetch_nodes_with_neighbors(neo4j_session, session_id)
    edges = fetch_edges(neo4j_session, session_id)

    if limit:
        node_rows = node_rows[:limit]
        allowed = {r["element_id"] for r in node_rows}
        edges = [
            e for e in edges
            if e["source_element_id"] in allowed and e["target_element_id"] in allowed
        ]

    print(f"  fetched {len(node_rows)} nodes, {len(edges)} edges for session_id={session_id}")

    composed = [(r, compose_text(r)) for r in node_rows]
    for r, text in composed[:show]:
        print(f"\n    ----- {r['props'].get('label')} (node_id={r['node_id']}) -----\n{text}")

    if dry_run:
        print(f"  [dry-run] skipped embedding/write for {len(composed)} nodes.\n")
        return

    payloads: list[dict[str, Any]] = []
    for i in range(0, len(composed), BATCH_SIZE):
        batch = composed[i : i + BATCH_SIZE]
        vectors = embed_batch(openai_client, [t for _, t in batch])
        for (r, text), vec in zip(batch, vectors):
            p = r["props"]
            payloads.append(
                {
                    "element_id": r["element_id"],
                    "node_id": r["node_id"],
                    "label": p.get("label") or "",
                    "entity_type": p.get("entity_type"),
                    "node_type": p.get("node_type"),
                    "composed_text": text,
                    "embedding": vec,
                    "props": p,
                }
            )
        print(f"    embedded {i + len(batch)}/{len(composed)}")

    write_nodes(pg_conn, session_id, payloads)
    write_edges(pg_conn, session_id, edges)
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.nodes WHERE session_id = %s", (session_id,))
        n_nodes = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.edges WHERE session_id = %s", (session_id,))
        n_edges = cur.fetchone()[0]
    print(f"  wrote {n_nodes} nodes, {n_edges} edges for session_id={session_id}.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--session-id",
        default=None,
        help="restrict to one BKGNode.session_id; omit to auto-discover and process every session",
    )
    ap.add_argument("--dry-run", action="store_true", help="print composed text, skip writes")
    ap.add_argument("--limit", type=int, default=0, help="cap to first N nodes per session (0 = all)")
    ap.add_argument("--show", type=int, default=2, help="print this many composed examples per session")
    args = ap.parse_args()

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            all_sessions = fetch_session_ids(s)
            if args.session_id:
                if args.session_id not in all_sessions:
                    print(
                        f"WARNING: --session-id={args.session_id} not found in Neo4j "
                        f"(discovered: {all_sessions}). Proceeding anyway."
                    )
                targets = [args.session_id]
            else:
                targets = all_sessions

            print(f"Neo4j DB `{NEO4J_DATABASE}` has {len(all_sessions)} distinct session_id(s): {all_sessions}")
            print(f"Will process {len(targets)} session(s): {targets}\n")

            if not targets:
                print("Nothing to do.")
                return 0

            pg_conn = None
            openai_client: OpenAI | None = None
            if not args.dry_run:
                pg_conn = pg_connect()
                ensure_schema(pg_conn)
                openai_client = OpenAI(api_key=OPENAI_API_KEY)

            try:
                for sid in targets:
                    print(f"===== session_id={sid} =====")
                    process_session(
                        s, pg_conn, openai_client, sid,
                        limit=args.limit, show=args.show, dry_run=args.dry_run,
                    )

                if pg_conn is not None:
                    with pg_conn.cursor() as cur:
                        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.nodes")
                        total_nodes = cur.fetchone()[0]
                        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.edges")
                        total_edges = cur.fetchone()[0]
                    print(
                        f"Postgres `{PG_DATABASE}` totals across all sessions: "
                        f"{total_nodes} nodes, {total_edges} edges."
                    )
            finally:
                if pg_conn is not None:
                    pg_conn.close()
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
