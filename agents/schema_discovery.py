"""
Schema Discovery node — Fetches the PostgreSQL table list once at the
start of each simulation run.

The KG schema context (nodes + paths) is now discovered per-traversal
via semantic embedding search, so this node only handles the static
table catalogue.
"""
from __future__ import annotations

import logging
from typing import Any

from models.state import SimulationState
from tools.bkg_tool import BKGTool

logger = logging.getLogger(__name__)


# ── Table List Helper ───────────────────────────────────────────────────────

def _fetch_table_list() -> str:
    """Fetch all available PostgreSQL tables from the KG and format as a prompt section."""
    try:
        bkg = BKGTool()
        result = bkg.query({"mode": "schema"})
        tables = result.get("tables", [])
        if not tables:
            return ""

        lines = ["\n\n=== AVAILABLE POSTGRESQL TABLES ==="]
        lines.append("Use ONLY these table names in SQL queries (use get_node on mapped nodes for full details):\n")

        for t in tables:
            name = t.get("table_name", "")
            db = t.get("database_name", "")
            nodes = t.get("nodes", [])
            node_ids = ", ".join(n.get("node_id", "") for n in nodes)
            key_cols = ", ".join(
                filter(None, set(n.get("key_column", "") for n in nodes))
            )

            detail = f"  nodes: {node_ids}" if node_ids else ""
            if key_cols:
                detail += f"  key_column(s): {key_cols}"
            if db:
                detail += f"  db: {db}"
            lines.append(f"  - {name}{detail}")

        lines.append("\nDo NOT invent table names. If you need a table not listed here, the data does not exist.")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to fetch table list: %s", e)
        return ""


# ── LangGraph Node ──────────────────────────────────────────────────────────

def discover_schema_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Fetch the static PostgreSQL table catalogue.

    KG node/path context is now resolved per-traversal via embedding search
    (in traversal_node / atraversal_node) so each sub-query gets its own
    tailored schema context.

    Reads: (none required)
    Writes: kg_schema (table list only), current_phase, messages
    """
    try:
        table_list = _fetch_table_list()

        logger.info("Table list discovered: %d chars", len(table_list))
        print(f"Table list is as follows: {table_list[:300]}")
        return {
            "kg_schema": table_list,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"PostgreSQL table list discovered ({len(table_list)} chars)",
            }],
        }
    except Exception as e:
        logger.error("Schema discovery failed: %s", e)
        return {
            "kg_schema": "",
            "current_phase": "traversal",
            "errors": [f"Schema discovery error: {e}"],
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Schema discovery failed: {e}",
            }],
        }
