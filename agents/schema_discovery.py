"""
Schema Discovery node — Discovers the Neo4j knowledge graph schema
once at the start of each simulation run. This provides context for
the Traversal Agent to explore the graph effectively.
"""
from __future__ import annotations

import logging
from typing import Any

from models.state import SimulationState
from tools.neo4j_tool import neo4j_tool

logger = logging.getLogger(__name__)


def discover_schema_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Discover KG schema.

    Reads: (nothing — runs first)
    Writes: kg_schema, messages
    """
    try:
        schema = neo4j_tool.get_schema()
        logger.info(f"Schema discovered: {len(schema)} chars")

        return {
            "kg_schema": schema,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Knowledge graph schema discovered ({len(schema)} chars)",
            }],
        }
    except Exception as e:
        logger.error(f"Schema discovery failed: {e}")
        # Provide a fallback empty schema — traversal agent will work with what it has
        return {
            "kg_schema": f"Schema discovery failed: {e}. Write generic Cypher queries.",
            "current_phase": "traversal",
            "errors": [f"Schema discovery error: {e}"],
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Schema discovery failed: {e}",
            }],
        }
