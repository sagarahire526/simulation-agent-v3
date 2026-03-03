"""
LangChain tool wrappers for the autonomous Traversal Agent.

Wraps existing tools (neo4j_tool, bkg_tool, python_sandbox) as
@tool functions that the ReAct agent can call.
"""
from __future__ import annotations

import json
from typing import Optional

from langchain_core.tools import tool

from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool
from tools.python_sandbox import execute_python, PythonSandbox


# Lazy singleton for BKGTool
_bkg: BKGTool | None = None


def _get_bkg() -> BKGTool:
    global _bkg
    if _bkg is None:
        _bkg = BKGTool()
    return _bkg


# ─────────────────────────────────────────────
# Neo4j Tools
# ─────────────────────────────────────────────

@tool
def run_cypher(query: str) -> str:
    """Execute a read-only Cypher query against the Neo4j Business Knowledge Graph.
    Use this for custom queries when the higher-level BKG tools don't cover your needs.
    Returns JSON with 'status', 'records', 'count', and 'elapsed_ms'.
    Only READ operations are allowed — no CREATE, MERGE, DELETE, SET, or REMOVE.
    """
    result = neo4j_tool.run_cypher_safe(query)
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# BKG High-Level Tools
# ─────────────────────────────────────────────

@tool
def get_node(node_id: str) -> str:
    """Fetch a single node from the Knowledge Graph by its node_id or metric_id.
    Returns all properties plus incoming and outgoing relationships.
    Supports aliases like 'GC' for GeneralContractor, 'NAS' for NASSession, etc.
    Use this when you know the exact node you want to inspect.
    """
    result = _get_bkg().query({"mode": "get_node", "node_id": node_id})
    return json.dumps(result, default=str)


@tool
def find_relevant(question: str) -> str:
    """Keyword search across all ConceptNodes and MetricNodes in the Knowledge Graph.
    Searches across node_id, name, definition, domain, and attributes fields.
    Returns up to 8 ConceptNodes and 5 MetricNodes, ranked by relevance score.
    Use this as your FIRST tool when you don't know which nodes to look at.
    """
    result = _get_bkg().query({"mode": "find_relevant", "question": question})
    return json.dumps(result, default=str)


@tool
def traverse_graph(start: str, depth: int = 2, rel_type: Optional[str] = None) -> str:
    """Walk the Knowledge Graph starting from a node, following relationships up to
    a given depth (1-4). Optionally filter by relationship type.
    Returns discovered paths and node details (definition, primary_table, base_query).
    Use this to explore the neighborhood of a concept — e.g., to find what tables,
    metrics, or related entities connect to a starting node.
    """
    req: dict = {"mode": "traverse", "start": start, "depth": depth}
    if rel_type:
        req["rel_type"] = rel_type
    result = _get_bkg().query(req)
    return json.dumps(result, default=str)


@tool
def get_diagnostic(metric_id: str) -> str:
    """Get detailed information about a MetricNode including its definition,
    SQL formulas, thresholds, unit, referenced nodes, and diagnostic traversal tree.
    Use this when you need to understand how a metric is computed or what drives it.
    """
    result = _get_bkg().query({"mode": "diagnostic", "metric_id": metric_id})
    return json.dumps(result, default=str)


@tool
def get_table_schema(table_name: str = "") -> str:
    """View the schema of database tables referenced in the Knowledge Graph.
    If table_name is provided, returns ConceptNodes linked to that table with their
    column_map, primary_key, grain, and base_query.
    If table_name is empty, returns an overview of ALL tables and which nodes use them.
    """
    req: dict = {"mode": "schema"}
    if table_name:
        req["table_name"] = table_name
    result = _get_bkg().query(req)
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# Python Sandbox Tools
# ─────────────────────────────────────────────

@tool
def run_python(code: str) -> str:
    """Execute Python code in a sandboxed environment for calculations.
    Available modules: math, json, statistics, collections, datetime, itertools, functools.
    Set a variable named 'result' to return structured data.
    Print statements will be captured as 'output'.
    Use this for arithmetic, aggregations, data transformations, or any computation
    that should not be done in your head.
    """
    result = execute_python(code)
    return json.dumps(result, default=str)


@tool
def run_sql_python(code: str, timeout_seconds: int = 30) -> str:
    """Execute Python code with access to a PostgreSQL database connection.
    Pre-imported: conn (psycopg2 read-only), pd (pandas), np (numpy),
    go (plotly.graph_objects), px (plotly.express), json.
    Set result = {...} to return data. DataFrames are auto-converted to records.
    Use this when you need to query PostgreSQL for actual operational data
    (as opposed to the Neo4j Knowledge Graph which describes the data model).
    """
    sandbox = PythonSandbox()
    result = sandbox.execute(code, timeout_seconds)
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────

def get_all_tools() -> list:
    """Return all tools for the traversal agent."""
    return [
        run_cypher,
        get_node,
        find_relevant,
        traverse_graph,
        get_diagnostic,
        get_table_schema,
        run_python,
        run_sql_python,
    ]