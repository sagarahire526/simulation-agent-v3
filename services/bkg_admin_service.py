"""
BKG Admin Service — read/write CRUD over the Business Knowledge Graph.

Backs the /api/v1/bkg-admin HTML interface. Two BKG graphs share the same
Neo4j database, partitioned by `session_id`. Every read and write is scoped
to a single session_id so the NAS graph and the telecom (NTM/AHLOB) graph
never bleed into each other.

Locked properties (never editable from the UI):
    node_id, session_id, entity_type, node_type, edge_id, embedding, status
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from neo4j import GraphDatabase

import config

logger = logging.getLogger(__name__)


# ── Graph registry ───────────────────────────────────────────────────────────
# Mirrors services.schema_embedding_service so the same session_ids are used
# everywhere. Exposed to the UI as a dropdown.
GRAPHS: dict[str, dict[str, str]] = {
    "ntm_ahlob": {
        "label": "NTM / AHLOB (Telecom)",
        "session_id": "69a3d22f26e208edc083a06e",
    },
    "nas": {
        "label": "NAS",
        "session_id": "6a079b6bc1ea92432985ef54",
    },
}


def list_graphs() -> list[dict[str, str]]:
    return [{"key": k, **v} for k, v in GRAPHS.items()]


def _resolve_session_id(graph_key: str) -> str:
    g = GRAPHS.get(graph_key)
    if g is None:
        raise ValueError(f"Unknown graph_key '{graph_key}'. Valid: {list(GRAPHS)}")
    return g["session_id"]


# ── Field whitelists ─────────────────────────────────────────────────────────
# Anything not listed here is silently dropped on update — keeps IDs,
# session_id, embeddings, etc. out of UI control.
LOCKED_FIELDS = frozenset({
    "node_id", "session_id", "entity_type", "node_type",
    "edge_id", "embedding", "status",
})

CORE_EDITABLE_FIELDS = [
    "name", "label", "definition", "nl_description", "nl_business_rule",
    "map_table_name", "map_database_name", "map_key_column",
    "map_label_column", "map_sql_template", "map_python_function",
    "map_contract",
]

KPI_EDITABLE_FIELDS = [
    "name", "label", "definition", "nl_description", "nl_business_rule",
    "kpi_name", "kpi_description", "kpi_formula_description",
    "kpi_python_function", "kpi_business_logic", "kpi_source_tables",
    "kpi_source_columns", "kpi_filters", "kpi_dimensions",
    "kpi_output_schema", "kpi_contract", "kpi_relationship_type",
]

ALL_EDITABLE_FIELDS = frozenset(CORE_EDITABLE_FIELDS + KPI_EDITABLE_FIELDS)


def editable_fields_for(entity_type: str) -> list[str]:
    return KPI_EDITABLE_FIELDS if entity_type == "kpi" else CORE_EDITABLE_FIELDS


# ── Driver (lazy singleton) ──────────────────────────────────────────────────
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )
        _driver.verify_connectivity()
    return _driver


def _run(cypher: str, **params) -> list[dict]:
    with _get_driver().session(database=config.NEO4J_DATABASE) as session:
        return [r.data() for r in session.run(cypher, **params)]


def _strip_internal(props: dict) -> dict:
    """Hide embeddings and other large/binary internals from the UI payload."""
    return {k: v for k, v in props.items() if k != "embedding"}


# ── Nodes: list ──────────────────────────────────────────────────────────────

def list_nodes(graph_key: str, entity_type: Optional[str] = None) -> list[dict]:
    """Return a lightweight list of nodes in the graph for the picker."""
    sid = _resolve_session_id(graph_key)
    if entity_type:
        rows = _run(
            """
            MATCH (n:BKGNode {session_id: $sid})
            WHERE n.entity_type = $et
            RETURN n.node_id     AS node_id,
                   n.name        AS name,
                   n.label       AS label,
                   n.entity_type AS entity_type
            ORDER BY n.label, n.node_id
            """,
            sid=sid, et=entity_type,
        )
    else:
        rows = _run(
            """
            MATCH (n:BKGNode {session_id: $sid})
            RETURN n.node_id     AS node_id,
                   n.name        AS name,
                   n.label       AS label,
                   n.entity_type AS entity_type
            ORDER BY n.entity_type, n.label, n.node_id
            """,
            sid=sid,
        )
    return rows


# ── Nodes: read one ──────────────────────────────────────────────────────────

def get_node(graph_key: str, node_id: str) -> dict:
    """Return full node properties plus incoming/outgoing relationships."""
    sid = _resolve_session_id(graph_key)
    rows = _run(
        """
        MATCH (n:BKGNode {session_id: $sid, node_id: $nid})
        RETURN n AS node
        """,
        sid=sid, nid=node_id,
    )
    if not rows:
        raise LookupError(f"Node '{node_id}' not found in graph '{graph_key}'")

    props = _strip_internal(dict(rows[0]["node"]))

    out_rows = _run(
        """
        MATCH (n:BKGNode {session_id: $sid, node_id: $nid})-[r:RELATES_TO]->(t:BKGNode {session_id: $sid})
        RETURN r.edge_id            AS edge_id,
               r.relationship_type  AS relationship_type,
               r.relationship       AS relationship,
               t.node_id            AS target_node_id,
               t.label              AS target_label,
               t.entity_type        AS target_entity_type
        """,
        sid=sid, nid=node_id,
    )
    in_rows = _run(
        """
        MATCH (s:BKGNode {session_id: $sid})-[r:RELATES_TO]->(n:BKGNode {session_id: $sid, node_id: $nid})
        RETURN r.edge_id            AS edge_id,
               r.relationship_type  AS relationship_type,
               r.relationship       AS relationship,
               s.node_id            AS source_node_id,
               s.label              AS source_label,
               s.entity_type        AS source_entity_type
        """,
        sid=sid, nid=node_id,
    )

    entity_type = props.get("entity_type") or "core"
    return {
        "properties": props,
        "editable_fields": editable_fields_for(entity_type),
        "locked_fields": sorted(LOCKED_FIELDS),
        "outgoing": out_rows,
        "incoming": in_rows,
    }


# ── Nodes: update business fields ────────────────────────────────────────────

def update_node(graph_key: str, node_id: str, updates: dict[str, Any]) -> dict:
    """Apply field updates, silently dropping any field not in the whitelist."""
    sid = _resolve_session_id(graph_key)
    safe = {k: v for k, v in updates.items() if k in ALL_EDITABLE_FIELDS}
    if not safe:
        raise ValueError("No editable fields supplied. Locked fields were ignored.")

    set_clauses = ", ".join(f"n.{k} = ${k}" for k in safe)
    cypher = f"""
        MATCH (n:BKGNode {{session_id: $sid, node_id: $nid}})
        SET {set_clauses}
        RETURN n AS node
    """
    rows = _run(cypher, sid=sid, nid=node_id, **safe)
    if not rows:
        raise LookupError(f"Node '{node_id}' not found in graph '{graph_key}'")
    return _strip_internal(dict(rows[0]["node"]))


# ── Nodes: create new ────────────────────────────────────────────────────────

def create_node(
    graph_key: str,
    node_id: str,
    entity_type: str,
    properties: dict[str, Any],
    relationships: list[dict],
) -> dict:
    """
    Insert a new BKGNode bound to this session, then attach the supplied
    relationships in the same transaction. Each relationship dict:
        {"direction": "out"|"in", "target_node_id": str,
         "relationship_type": str, "relationship": str (optional human form)}

    Enforces:
      - node_id is unique within session_id
      - entity_type ∈ valid set
      - kpi → kpi relationships rejected (in either direction)
      - every target node must exist in the same session
    """
    sid = _resolve_session_id(graph_key)

    valid_types = {"core", "context", "transaction", "reference", "kpi"}
    if entity_type not in valid_types:
        raise ValueError(f"entity_type must be one of {sorted(valid_types)}")
    if not node_id or not node_id.strip():
        raise ValueError("node_id is required")

    # Uniqueness check within session
    exists = _run(
        "MATCH (n:BKGNode {session_id: $sid, node_id: $nid}) RETURN n LIMIT 1",
        sid=sid, nid=node_id,
    )
    if exists:
        raise ValueError(f"Node '{node_id}' already exists in this graph")

    # Validate every relationship up-front so we don't half-create
    cleaned_rels: list[dict] = []
    for r in relationships or []:
        direction = r.get("direction", "out")
        target = r.get("target_node_id")
        rel_type = (r.get("relationship_type") or "").strip()
        rel_human = (r.get("relationship") or "").strip() or rel_type.lower().replace("_", " ")

        if direction not in ("out", "in"):
            raise ValueError(f"relationship.direction must be 'out' or 'in', got {direction!r}")
        if not target:
            raise ValueError("relationship.target_node_id is required")
        if not rel_type:
            raise ValueError("relationship.relationship_type is required")

        # Look up the target's entity_type in this session
        target_rows = _run(
            "MATCH (t:BKGNode {session_id: $sid, node_id: $tid}) "
            "RETURN t.entity_type AS et",
            sid=sid, tid=target,
        )
        if not target_rows:
            raise ValueError(
                f"Target node '{target}' does not exist in this graph"
            )
        target_et = target_rows[0]["et"]

        # kpi ↔ kpi is forbidden, regardless of direction
        if entity_type == "kpi" and target_et == "kpi":
            raise ValueError(
                f"Invalid relationship: kpi→kpi is not allowed "
                f"(new node and '{target}' are both KPIs)"
            )

        cleaned_rels.append({
            "direction": direction,
            "target": target,
            "relationship_type": rel_type,
            "relationship": rel_human,
        })

    # Build the node property bag from whitelisted fields plus the mandatory
    # internal fields. Anything outside the whitelist is dropped silently.
    safe_props = {k: v for k, v in (properties or {}).items() if k in ALL_EDITABLE_FIELDS}
    safe_props.update({
        "node_id": node_id,
        "session_id": sid,
        "entity_type": entity_type,
        "node_type": entity_type.upper(),
        "status": "confirmed",
    })

    _run(
        "CREATE (n:BKGNode) SET n = $props",
        props=safe_props,
    )

    # Attach relationships
    created_edges = []
    for r in cleaned_rels:
        edge_id = str(uuid.uuid4())
        if r["direction"] == "out":
            cypher = """
                MATCH (a:BKGNode {session_id: $sid, node_id: $src}),
                      (b:BKGNode {session_id: $sid, node_id: $tgt})
                CREATE (a)-[e:RELATES_TO {
                    edge_id: $eid,
                    relationship: $rel,
                    relationship_type: $rtype,
                    session_id: $sid,
                    status: 'confirmed',
                    style: 'solid'
                }]->(b)
                RETURN e.edge_id AS edge_id
            """
            _run(
                cypher,
                sid=sid, src=node_id, tgt=r["target"],
                eid=edge_id, rel=r["relationship"], rtype=r["relationship_type"],
            )
        else:  # "in"
            cypher = """
                MATCH (a:BKGNode {session_id: $sid, node_id: $src}),
                      (b:BKGNode {session_id: $sid, node_id: $tgt})
                CREATE (a)-[e:RELATES_TO {
                    edge_id: $eid,
                    relationship: $rel,
                    relationship_type: $rtype,
                    session_id: $sid,
                    status: 'confirmed',
                    style: 'solid'
                }]->(b)
                RETURN e.edge_id AS edge_id
            """
            _run(
                cypher,
                sid=sid, src=r["target"], tgt=node_id,
                eid=edge_id, rel=r["relationship"], rtype=r["relationship_type"],
            )
        created_edges.append(edge_id)

    return {
        "node_id": node_id,
        "session_id": sid,
        "entity_type": entity_type,
        "created_edges": created_edges,
    }


# ── Relationships on existing nodes ──────────────────────────────────────────

def add_relationship(
    graph_key: str,
    source_node_id: str,
    target_node_id: str,
    relationship_type: str,
    relationship: Optional[str] = None,
) -> dict:
    """Attach a new RELATES_TO edge between two existing nodes in this graph."""
    sid = _resolve_session_id(graph_key)
    rel_type = (relationship_type or "").strip()
    if not rel_type:
        raise ValueError("relationship_type is required")
    rel_human = (relationship or "").strip() or rel_type.lower().replace("_", " ")

    rows = _run(
        "MATCH (n:BKGNode {session_id: $sid}) WHERE n.node_id IN [$a, $b] "
        "RETURN n.node_id AS node_id, n.entity_type AS et",
        sid=sid, a=source_node_id, b=target_node_id,
    )
    by_id = {r["node_id"]: r["et"] for r in rows}
    if source_node_id not in by_id:
        raise ValueError(f"Source node '{source_node_id}' not found in this graph")
    if target_node_id not in by_id:
        raise ValueError(f"Target node '{target_node_id}' not found in this graph")
    if by_id[source_node_id] == "kpi" and by_id[target_node_id] == "kpi":
        raise ValueError("Invalid relationship: kpi→kpi is not allowed")

    edge_id = str(uuid.uuid4())
    _run(
        """
        MATCH (a:BKGNode {session_id: $sid, node_id: $src}),
              (b:BKGNode {session_id: $sid, node_id: $tgt})
        CREATE (a)-[e:RELATES_TO {
            edge_id: $eid,
            relationship: $rel,
            relationship_type: $rtype,
            session_id: $sid,
            status: 'confirmed',
            style: 'solid'
        }]->(b)
        """,
        sid=sid, src=source_node_id, tgt=target_node_id,
        eid=edge_id, rel=rel_human, rtype=rel_type,
    )
    return {"edge_id": edge_id, "relationship_type": rel_type, "relationship": rel_human}


def delete_node(graph_key: str, node_id: str) -> dict:
    """
    Delete a node and every relationship attached to it (DETACH DELETE),
    scoped to the graph's session_id. Returns counts for confirmation.
    """
    sid = _resolve_session_id(graph_key)
    existing = _run(
        "MATCH (n:BKGNode {session_id: $sid, node_id: $nid}) RETURN n LIMIT 1",
        sid=sid, nid=node_id,
    )
    if not existing:
        raise LookupError(f"Node '{node_id}' not found in graph '{graph_key}'")

    rel_count = _run(
        """
        MATCH (n:BKGNode {session_id: $sid, node_id: $nid})-[r]-()
        RETURN count(r) AS c
        """,
        sid=sid, nid=node_id,
    )[0]["c"]

    _run(
        "MATCH (n:BKGNode {session_id: $sid, node_id: $nid}) DETACH DELETE n",
        sid=sid, nid=node_id,
    )
    return {"deleted_node": node_id, "deleted_relationships": rel_count}


def delete_relationship(graph_key: str, edge_id: str) -> bool:
    """Remove a single relationship by edge_id, scoped to the graph."""
    sid = _resolve_session_id(graph_key)
    existing = _run(
        "MATCH ()-[r:RELATES_TO {session_id: $sid, edge_id: $eid}]->() "
        "RETURN r LIMIT 1",
        sid=sid, eid=edge_id,
    )
    if not existing:
        return False
    _run(
        "MATCH ()-[r:RELATES_TO {session_id: $sid, edge_id: $eid}]->() DELETE r",
        sid=sid, eid=edge_id,
    )
    return True
