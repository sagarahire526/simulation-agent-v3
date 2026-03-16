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

    def get_schema(self) -> str:
        """
        Discover the KG schema structure only: node labels with property
        names/types, relationship types with property names/types, and
        relationship patterns.  No sample data, counts, indexes, or
        constraints — keeps the prompt lightweight.
        """
        db = config.neo4j.database

        with self.driver.session(database=db) as session:
            # ── 1. Node labels + property names/types ──
            node_info = session.run(
                "CALL db.schema.nodeTypeProperties() "
                "YIELD nodeType, propertyName, propertyTypes, mandatory "
                "RETURN nodeType, "
                "  collect({name: propertyName, types: propertyTypes, mandatory: mandatory}) "
                "  AS properties"
            ).data()

            # ── 2. Relationship types + property names/types ──
            rel_info = session.run(
                "CALL db.schema.relTypeProperties() "
                "YIELD relType, propertyName, propertyTypes, mandatory "
                "RETURN relType, "
                "  collect({name: propertyName, types: propertyTypes, mandatory: mandatory}) "
                "  AS properties"
            ).data()

            # ── 3. Relationship patterns: (SourceLabel)-[:REL_TYPE]->(TargetLabel) ──
            rel_patterns = session.run(
                "MATCH (a)-[r]->(b) "
                "WITH labels(a) AS srcLabels, type(r) AS relType, labels(b) AS tgtLabels "
                "RETURN DISTINCT srcLabels, relType, tgtLabels "
                "ORDER BY relType"
            ).data()

        # ── Build formatted output ──
        schema_lines = ["=== KNOWLEDGE GRAPH SCHEMA ===\n"]

        # -- Node Labels & Properties --
        schema_lines.append("── Node Labels & Properties ──")
        for row in node_info:
            node_type = row["nodeType"]
            props_list = []
            for p in row["properties"]:
                if not p["name"]:
                    continue
                types_str = "/".join(p["types"]) if p["types"] else "Unknown"
                req = " (required)" if p.get("mandatory") else ""
                props_list.append(f"{p['name']}: {types_str}{req}")
            schema_lines.append(f"  {node_type}")
            for prop in props_list:
                schema_lines.append(f"    - {prop}")
            if not props_list:
                schema_lines.append("    (no properties)")

        # -- Relationship Types & Properties --
        schema_lines.append("\n── Relationship Types & Properties ──")
        for row in rel_info:
            rel_type = row["relType"]
            props_list = []
            for p in row["properties"]:
                if not p["name"]:
                    continue
                types_str = "/".join(p["types"]) if p["types"] else "Unknown"
                req = " (required)" if p.get("mandatory") else ""
                props_list.append(f"{p['name']}: {types_str}{req}")
            schema_lines.append(f"  {rel_type}")
            for prop in props_list:
                schema_lines.append(f"    - {prop}")
            if not props_list:
                schema_lines.append("    (no properties)")

        # -- Relationship Patterns --
        schema_lines.append("\n── Relationship Patterns ──")
        for row in rel_patterns:
            src = ":".join(row["srcLabels"])
            tgt = ":".join(row["tgtLabels"])
            schema_lines.append(f"  (:{src})-[:{row['relType']}]->(:{tgt})")

        logger.debug("Schema discovery complete: %d lines", len(schema_lines))
        # print(f"SCHEMA LINES ARE AS FOLLOWS: {schema_lines}")
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