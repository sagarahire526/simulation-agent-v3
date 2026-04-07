"""
Schema Discovery node — Discovers the Neo4j knowledge graph schema
once at the start of each simulation run. This provides context for
the Traversal Agent to explore the graph effectively.

Includes an LLM-powered pre-filter that selects only query-relevant
nodes for full display, keeping the schema prompt lightweight.
"""
from __future__ import annotations

import json
import re
import logging
from typing import Any

from models.state import SimulationState
from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool
from services.llm_provider import LLMProvider

logger = logging.getLogger(__name__)


# ── LLM-Powered Schema Pre-Filter ──────────────────────────────────────────

def _select_relevant_nodes(query: str, all_nodes: list[dict]) -> set[str] | None:
    """
    Use a fast LLM to select query-relevant node_ids from the full node list.

    Returns a set of node_ids, or None if the selection fails or yields
    too few results (triggering fallback to full schema).
    """
    if not query or not all_nodes:
        return None

    # Build compact node list for the LLM (~3 tokens per node)
    node_lines = []
    for n in all_nodes:
        nid = n.get("node_id", "")
        et = n.get("entity_type", "unknown")
        label = n.get("label", nid)
        defn = (n.get("definition") or "")[:80]
        line = f"[{et}] {label} ({nid})"
        if defn:
            line += f" — {defn}"
        node_lines.append(line)

    prompt = (
        f'Given this user query about telecom data:\n"{query}"\n\n'
        "Select the 10 most relevant node_ids from this Knowledge Graph "
        "that would be needed to answer the query.\n"
        "Include: directly relevant nodes, data source nodes, and related dimensional nodes.\n\n"
        "Available nodes:\n"
        + "\n".join(node_lines)
        + '\n\nReturn ONLY a JSON array of node_id strings. Example: ["node_a", "node_b"]'
    )

    try:
        llm = LLMProvider.get_llm("fast", temperature=0, max_tokens=300)
        response = llm.invoke(prompt)
        text = response.content.strip()

        # Extract JSON array even if wrapped in markdown code fences
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if match:
            selected = json.loads(match.group())
            if isinstance(selected, list) and len(selected) >= 3:
                result = set(str(s) for s in selected)
                logger.info(
                    "Schema pre-filter: LLM selected %d relevant nodes", len(result)
                )
                return result
        logger.warning("Schema pre-filter: LLM returned too few nodes, using full schema")
        return None
    except Exception as e:
        logger.warning("Schema pre-filter failed, using full schema: %s", e)
        return None


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
    LangGraph node: Discover KG schema + available PostgreSQL tables.

    When a user query is available, uses an LLM pre-filter to select
    only relevant nodes for full display. Falls back to full schema
    if the filter fails or yields too few results.

    Reads: refined_query, user_query
    Writes: kg_schema, messages
    """
    try:
        query = state.get("refined_query") or state.get("user_query", "")

        # LLM pre-filter: select query-relevant nodes
        relevant_ids = None
        if query:
            try:
                all_nodes = neo4j_tool.get_all_nodes()
                relevant_ids = _select_relevant_nodes(query, all_nodes)
            except Exception as e:
                logger.warning("Node pre-filter skipped: %s", e)

        schema = neo4j_tool.get_schema(relevant_ids=relevant_ids)
        table_list = _fetch_table_list()
        full_schema = schema + table_list

        filter_note = (
            f", filtered to {len(relevant_ids)} relevant nodes"
            if relevant_ids else ", full schema (no filter)"
        )
        logger.info(f"Schema discovered: {len(full_schema)} chars{filter_note}")
        # print(f"FINAL LENGTH OF KG SCHEMA IS AS FOLLOWS: {full_schema}")

        return {
            "kg_schema": full_schema,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": (
                    f"Knowledge graph schema discovered ({len(full_schema)} chars"
                    f"{filter_note})"
                ),
            }],
        }
    except Exception as e:
        logger.error(f"Schema discovery failed: {e}")
        return {
            "kg_schema": f"Schema discovery failed: {e}. Write generic Cypher queries.",
            "current_phase": "traversal",
            "errors": [f"Schema discovery error: {e}"],
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Schema discovery failed: {e}",
            }],
        }
