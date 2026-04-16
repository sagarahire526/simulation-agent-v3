"""
Neo4j Knowledge Graph tools for the Traversal Agent.
Handles connection, schema discovery, and query execution.
"""
from __future__ import annotations

import time
import logging
from typing import Any, Optional

from neo4j import GraphDatabase, Driver

from config.settings import config

logger = logging.getLogger(__name__)

# Suppress noisy Neo4j deprecation/notification warnings
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
logging.getLogger("neo4j.io").setLevel(logging.WARNING)


class Neo4jTool:
    """Manages Neo4j connections and query execution."""

    def __init__(self):
        self._driver: Optional[Driver] = None

    @property
    def driver(self) -> Driver:
        if self._driver is None:
            cfg = config.neo4j
            self._driver = GraphDatabase.driver(
                cfg.uri,
                auth=(cfg.user, cfg.password),
            )
            # Verify connectivity
            self._driver.verify_connectivity()
            logger.info(f"Connected to Neo4j at {cfg.uri}, db={cfg.database}")
        return self._driver

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    # ─────────────────────────────────────────────
    # Schema Discovery
    # ─────────────────────────────────────────────

    def get_all_nodes(self) -> list[dict]:
        """Return all BKGNode instances as dicts with node_id, label, entity_type, definition."""
        db = config.neo4j.database
        with self.driver.session(database=db) as session:
            return session.run(
                "MATCH (n:BKGNode) "
                "RETURN n.entity_type AS entity_type, n.node_id AS node_id, "
                "       n.label AS label, n.definition AS definition "
                "ORDER BY n.entity_type, n.node_id"
            ).data()

    def get_schema(self) -> str:
        """
        Discover the KG schema — optimised for minimal token usage.

        Omits raw property listings and relationship type metadata
        (the agent gets those from get_kpi / get_node calls).
        Deduplicates and groups relationships by source node.
        """
        db = config.neo4j.database

        with self.driver.session(database=db) as session:
            # ── 1. All BKGNode instances with definitions ──
            node_instances = session.run(
                "MATCH (n:BKGNode) "
                "RETURN n.entity_type AS entity_type, "
                "       n.node_id AS node_id, "
                "       n.label AS label, "
                "       coalesce(n.definition, '') AS definition "
                "ORDER BY n.entity_type, n.node_id"
            ).data()

            # ── 2. Actual relationship map between nodes ──
            node_relationships = session.run(
                "MATCH (a:BKGNode)-[r:RELATES_TO]->(b:BKGNode) "
                "RETURN DISTINCT a.node_id AS source, "
                "       r.relationship_type AS rel_type, "
                "       b.node_id AS target "
                "ORDER BY source"
            ).data()

        # ── Build formatted output ──
        from collections import defaultdict

        schema_lines = ["=== KNOWLEDGE GRAPH SCHEMA ===\n"]

        # -- BKG Nodes by entity type --
        schema_lines.append("── BKG Nodes (by entity type) ──")
        current_type = None
        for row in node_instances:
            et = row.get("entity_type", "unknown")
            if et != current_type:
                current_type = et
                schema_lines.append(f"\n  [{et}]")
            label_str = f" — {row['label']}" if row.get("label") else ""
            def_str = f" : {row['definition']}" if row.get("definition") else ""
            schema_lines.append(f"    • {row['node_id']}{label_str}{def_str}")

        # -- Deduplicated & grouped relationships --
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        seen: set[tuple[str, str, str]] = set()
        for row in node_relationships:
            key = (row["source"], row.get("rel_type") or "RELATES_TO", row["target"])
            if key not in seen:
                seen.add(key)
                grouped[(key[0], key[1])].append(key[2])

        schema_lines.append("\n── Node Relationships ──")
        for (source, rel_type), targets in grouped.items():
            schema_lines.append(
                f"  ({source}) —[{rel_type}]→ {', '.join(targets)}"
            )

        logger.debug("Schema discovery complete: %d lines", len(schema_lines))
        return "\n".join(schema_lines)

    # ─────────────────────────────────────────────
    # Query Execution
    # ─────────────────────────────────────────────

    def run_cypher(self, query: str, params: dict[str, Any] | None = None) -> dict:
        """
        Execute a Cypher query and return results + metadata.
        """
        db = config.neo4j.database
        params = params or {}

        start = time.perf_counter()
        try:
            with self.driver.session(database=db) as session:
                result = session.run(query, params)
                records = [record.data() for record in result]
                summary = result.consume()

            elapsed_ms = (time.perf_counter() - start) * 1000

            return {
                "status": "success",
                "records": records,
                "count": len(records),
                "elapsed_ms": round(elapsed_ms, 2),
                "query": query,
            }
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error(f"Cypher error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "elapsed_ms": round(elapsed_ms, 2),
                "query": query,
                "records": [],
                "count": 0,
            }

    def run_cypher_safe(self, query: str, params: dict[str, Any] | None = None) -> dict:
        """
        Execute a read-only Cypher query (rejects writes).
        """
        # Basic write-guard
        upper = query.upper().strip()
        write_keywords = ["CREATE", "MERGE", "DELETE", "DETACH", "SET ", "REMOVE "]
        for kw in write_keywords:
            if kw in upper and not upper.startswith("//"):
                return {
                    "status": "error",
                    "error": f"Write operations not allowed. Detected: {kw.strip()}",
                    "records": [],
                    "count": 0,
                    "query": query,
                }
        return self.run_cypher(query, params)


# Singleton
neo4j_tool = Neo4jTool()