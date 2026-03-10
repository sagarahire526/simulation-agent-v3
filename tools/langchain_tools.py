"""
LangChain tool wrappers for the autonomous Traversal Agent.

Wraps existing tools (neo4j_tool, bkg_tool, python_sandbox) as
@tool functions that the ReAct agent can call.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from langchain_core.tools import tool

from tools.bkg_tool import BKGTool
from tools.neo4j_tool import Neo4jTool
from tools.python_sandbox import execute_python, PythonSandbox


# Thread-local storage: each thread (including each parallel planner sub-traversal)
# gets its own BKGTool and Neo4jTool instance, preventing shared-connection serialization.
_local = threading.local()


def _get_bkg() -> BKGTool:
    if not hasattr(_local, "bkg"):
        _local.bkg = BKGTool()
    return _local.bkg


def _get_neo4j() -> Neo4jTool:
    if not hasattr(_local, "neo4j"):
        _local.neo4j = Neo4jTool()
    return _local.neo4j


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
    result = _get_neo4j().run_cypher_safe(query)
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
    Pre-available (no import needed): math, json, statistics, np (numpy), pd (pandas)
    Also importable: collections, datetime, itertools, functools.
    Set a variable named 'result' to return structured data.
    Print statements will be captured as 'output'.
    Use this for arithmetic, aggregations, data transformations, or any computation
    that should not be done in your head.
    Do NOT write import statements for pre-available modules — they are already loaded.

    IMPORTANT — SQL SCHEMA RULE: When writing any SQL query, ALWAYS prefix table names
    with the schema: pwc_macro_staging_schema.<table_name>
    Example: SELECT * FROM pwc_macro_staging_schema.site_data

    ON FAILURE: The full error message and traceback will be returned — read the ENTIRE
    error, diagnose the root cause, fix the code, and call this tool again.
    You MUST retry up to 3 times before giving up. Do NOT stop after a single failure.
    """
    try:
        result = execute_python(code)
    except Exception as exc:
        import traceback
        result = {"status": "error", "error": str(exc), "traceback": traceback.format_exc(), "output": ""}

    if result.get("status") == "error":
        result["retry_instruction"] = (
            "EXECUTION FAILED. Read the FULL 'error' and 'traceback' fields below, "
            "diagnose the issue, fix the code, and call run_python again. "
            "You MUST retry up to 3 times before giving up."
        )
    return json.dumps(result, default=str)


@tool
def run_sql_python(code: str, timeout_seconds: int = 30) -> str:
    """Execute PYTHON code (not raw SQL) with access to a PostgreSQL database connection.
    Pre-imported: conn (psycopg2 read-only), pd (pandas), np (numpy),
    go (plotly.graph_objects), px (plotly.express), json.
    Set result = {...} to return data. DataFrames are auto-converted to records.
    Use this when you need to query PostgreSQL for actual operational data
    (as opposed to the Neo4j Knowledge Graph which describes the data model).

    CRITICAL — YOU MUST WRAP SQL IN pd.read_sql(). Never pass raw SQL directly.
    CORRECT:   result = pd.read_sql("SELECT * FROM pwc_macro_staging_schema.site_data", conn).to_dict(orient="records")
    WRONG:     SELECT * FROM pwc_macro_staging_schema.site_data

    IMPORTANT — SQL SCHEMA RULE: ALWAYS prefix table names with the schema:
    pwc_macro_staging_schema.<table_name>

    ON FAILURE: The full error message and traceback will be returned — read the ENTIRE
    error, diagnose the root cause, fix the SQL or Python, and call this tool again.
    Common fixes: wrong column name → check get_table_schema first;
    syntax error → fix the Python/SQL; connection error → simplify the query.
    You MUST retry up to 3 times before giving up. Do NOT stop after a single failure.
    """
    try:
        sandbox = PythonSandbox()
        result = sandbox.execute(code, timeout_seconds)
    except Exception as exc:
        import traceback
        result = {"status": "error", "error": str(exc), "traceback": traceback.format_exc(), "output": ""}

    if result.get("status") == "error":
        result["retry_instruction"] = (
            "EXECUTION FAILED. Read the FULL 'error' and 'traceback' fields below, "
            "diagnose the issue, fix the code, and call run_sql_python again. "
            "You MUST retry up to 3 times before giving up."
        )
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