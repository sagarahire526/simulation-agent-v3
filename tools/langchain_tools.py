"""
LangChain tool wrappers for the autonomous Traversal Agent.

Wraps existing tools (neo4j_tool, bkg_tool, python_sandbox) as
@tool functions that the ReAct agent can call.

Tools are ordered by recommended usage sequence (KPI-first):
  1. get_kpi        — FIRST CHOICE: KPI formula, logic, python function, source tables
  2. get_node       — FALLBACK: core node map_* properties when KPI is insufficient
  3. find_relevant  — ONLY when schema doesn't reveal the right nodes
  4. traverse_graph — ONLY when schema relationship map is insufficient
  5. run_sql_python — query PostgreSQL with Python
  6. run_python     — sandboxed calculations
  7. run_cypher     — read-only Neo4j Cypher (last resort)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_core.tools import tool

from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool
from tools.python_sandbox import execute_python, PythonSandbox

logger = logging.getLogger(__name__)

# Lazy singleton for BKGTool
_bkg: BKGTool | None = None


def _get_bkg() -> BKGTool:
    global _bkg
    if _bkg is None:
        _bkg = BKGTool()
    return _bkg


# ─────────────────────────────────────────────
# Tool output truncation — prevents context overflow
# ─────────────────────────────────────────────
# Per-tool char limits for what gets returned to the LLM.
# get_kpi is generous because kpi_python_function must stay intact for SQL writing.
# Data tools (run_sql_python, run_cypher) are capped tighter — the agent only needs
# sample rows + counts, not the full dataset.

_TOOL_CHAR_LIMITS = {
    "get_kpi":        50000,
    "get_node":       50000,
    "find_relevant":  6000,
    "traverse_graph": 6000,
    "run_sql_python": 10000,
    "run_python":     10000,
    "run_cypher":     6000,
}


def _truncate_tool_output(tool_name: str, raw_json: str) -> str:
    """
    Truncate a tool's JSON output to fit within the tool's char budget.
    Preserves structure: for list results, keeps first N rows + total count.
    For errors, always returns full output (errors are small and needed for retry).

    This is the primary defense against context overflow. Each parallel
    traversal agent has its own message history — truncation is local,
    no cross-agent interference.
    """
    limit = _TOOL_CHAR_LIMITS.get(tool_name, 3000)

    if len(raw_json) <= limit:
        return raw_json

    # Try to intelligently truncate structured data
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        # Not JSON — hard truncate
        return raw_json[:limit] + '\n... (truncated by tool trimmer)'

    if isinstance(parsed, dict):
        # Errors: always keep full
        if parsed.get("status") == "error" or "error" in parsed:
            return raw_json

        # run_sql_python / run_python: truncate the 'result' list
        if "result" in parsed and isinstance(parsed["result"], list):
            rows = parsed["result"]
            total = len(rows)
            # Binary search for how many rows fit
            keep = total
            while keep > 0:
                parsed["result"] = rows[:keep]
                parsed["_truncated"] = {
                    "total_rows": total,
                    "rows_shown": keep,
                    "message": f"Showing {keep} of {total} rows. Use aggregations/GROUP BY to reduce."
                }
                candidate = json.dumps(parsed, default=str)
                if len(candidate) <= limit:
                    return candidate
                keep = keep // 2
            # Even 0 rows too big — shouldn't happen but fallback
            parsed["result"] = []
            parsed["_truncated"] = {"total_rows": total, "rows_shown": 0}
            return json.dumps(parsed, default=str)[:limit]

        # run_cypher: truncate 'records' list
        if "records" in parsed and isinstance(parsed["records"], list):
            rows = parsed["records"]
            total = len(rows)
            keep = total
            while keep > 0:
                parsed["records"] = rows[:keep]
                parsed["count"] = total
                parsed["_truncated"] = f"Showing {keep} of {total} records"
                candidate = json.dumps(parsed, default=str)
                if len(candidate) <= limit:
                    return candidate
                keep = keep // 2

        # get_kpi / get_node / find_relevant: truncate large string fields
        compact = json.dumps(parsed, default=str)
        if len(compact) <= limit:
            return compact
        return compact[:limit] + '\n... (truncated by tool trimmer)'

    # Fallback: hard truncate
    return raw_json[:limit] + '\n... (truncated by tool trimmer)'


# ─────────────────────────────────────────────
# Neo4j Tools
# ─────────────────────────────────────────────

@tool
def run_cypher(query: str) -> str:
    """Execute a read-only Cypher query against the Neo4j Business Knowledge Graph.

    USE WHEN: You need a custom query that the higher-level tools (find_relevant,
    get_node, traverse_graph, get_kpi) cannot handle — e.g., aggregations across
    multiple node types or filtering by specific property values.

    IMPORTANT:
    - All nodes use the `BKGNode` label with a `node_id` property.
    - Use `entity_type` to filter: 'core', 'context', 'transaction', 'reference', 'kpi'.
    - Relationships are `RELATES_TO` edges with a `relationship_type` property.
    - Only READ operations are allowed (no CREATE, MERGE, DELETE, SET, REMOVE).

    RETURNS: JSON with 'status', 'records', 'count', and 'elapsed_ms'.
    """
    result = neo4j_tool.run_cypher_safe(query)
    return _truncate_tool_output("run_cypher", json.dumps(result, default=str))


# ─────────────────────────────────────────────
# BKG High-Level Tools
# ─────────────────────────────────────────────

@tool
def get_node(node_id: str) -> str:
    """FALLBACK — Fetch a core/context/transaction node's database mapping details.

    USE ONLY WHEN: get_kpi did not return adequate logic/formulas for your query,
    and you need the core node's map_* properties (map_table_name, map_python_function,
    map_contract, map_key_column, map_label_column, map_database_name).

    DO NOT use this tool if get_kpi already gave you the source tables and python function.

    Returns: node_id, name, label, entity_type, definition, nl_description,
    map_* properties, plus outgoing and incoming relationships.
    Supports aliases: 'GC' → general_contractor, 'BOM' → bill_of_materials, etc.
    """
    result = _get_bkg().query({"mode": "get_node", "node_id": node_id})
    return _truncate_tool_output("get_node", json.dumps(result, default=str))


@tool
def find_relevant(question: str) -> str:
    """Keyword search across all BKGNode nodes — use ONLY when the KG schema
    doesn't reveal the right nodes for your query.

    The schema already lists all nodes and relationships. Check it FIRST.
    Only call this if the query uses terms that don't match any node_id or label.

    SEARCHES: node_id, name, label, definition, nl_description, entity_type,
    kpi_name, kpi_description.

    RETURNS: Up to 10 nodes ranked by relevance, with node_id, entity_type,
    definition, and neighbor preview.
    """
    result = _get_bkg().query({"mode": "find_relevant", "question": question})
    return _truncate_tool_output("find_relevant", json.dumps(result, default=str))


@tool
def traverse_graph(start: str, depth: int = 2, rel_type: Optional[str] = None) -> str:
    """Walk the Knowledge Graph from a starting node, following relationships up to
    a given depth (1-4). Optionally filter by relationship_type.

    USE WHEN: You need to explore what connects to a node — e.g., find related
    tables, KPIs, or entities that are linked through the graph.

    PARAMETERS:
    - start: node_id to start from (aliases like 'GC' are resolved automatically)
    - depth: how many hops to traverse (1-4, default 2)
    - rel_type: optional filter — only follow edges with this relationship_type
      (e.g., 'COMPUTES_FROM', 'SUPPLIES', 'HAS_PREREQUISITE')

    RETURNS: paths (from → relationship → to) and discovered_nodes with their
    entity_type, label, definition, map_table_name, and kpi_name.
    """
    req: dict = {"mode": "traverse", "start": start, "depth": depth}
    if rel_type:
        req["rel_type"] = rel_type
    result = _get_bkg().query(req)
    return _truncate_tool_output("traverse_graph", json.dumps(result, default=str))


@tool
def get_kpi(node_id: str) -> str:
    """YOUR FIRST TOOL — Get KPI computation details including connected core nodes.

    ALWAYS call this BEFORE get_node. KPI nodes contain:
    - What it measures (kpi_description, kpi_formula_description)
    - How to compute it (kpi_business_logic, kpi_python_function)
    - What data it needs (kpi_source_tables, kpi_source_columns, kpi_dimensions)
    - How to filter it (kpi_filters)
    - What it outputs (kpi_output_schema)
    - Its function contract (kpi_contract)
    - Related core node IDs and their table mappings

    ⚠️ THIS RETURNS METADATA ONLY — NOT actual data. After calling this, you MUST
    follow up with run_sql_python to execute the kpi_python_function and get real numbers.
    get_kpi tells you HOW to query; run_sql_python actually RUNS the query.

    MANDATORY SEQUENCE: get_kpi → run_sql_python (with full function code from kpi_python_function)

    ALSO: If the node_id is a core/context node (not a KPI), returns all KPI nodes
    that reference or compute from it.
    """
    result = _get_bkg().query({"mode": "get_kpi", "node_id": node_id})
    return _truncate_tool_output("get_kpi", json.dumps(result, default=str))



# ─────────────────────────────────────────────
# Python Sandbox Tools
# ─────────────────────────────────────────────

@tool
def run_python(code: str) -> str:
    """Execute Python code in a sandboxed environment for calculations.

    USE WHEN: You need to perform arithmetic, aggregations, data transformations,
    or any computation that must NOT be done in your head.

    AVAILABLE: math, json, statistics, collections, datetime, itertools, functools,
    numpy (as np), pandas (as pd).

    RULES:
    - Set `result = <value>` at the end to return data. A bare variable name does
      NOT return data.
    - Print statements are captured as 'output'.
    - On error: read the FULL 'error' and 'traceback' fields carefully, diagnose
      the root cause, fix your code, and call this tool again with corrected code.
      You may retry up to 3 times total — each retry must have a meaningful fix.

    RETURNS: JSON with 'status', 'output' (stdout), 'result' (your result variable),
    and 'elapsed_ms'. On error: 'error' and 'traceback' fields.
    """
    result = execute_python(code)
    return _truncate_tool_output("run_python", json.dumps(result, default=str))


@tool
def run_sql_python(code: str, timeout_seconds: int = 30) -> str:
    """Execute Python code with access to a read-only PostgreSQL database connection.

    USE WHEN: You need to query the PostgreSQL database for actual operational data.
    The Neo4j Knowledge Graph describes the data MODEL; this tool queries the
    actual DATA.

    PRE-IMPORTED: conn (psycopg2 read-only), pd (pandas), np (numpy),
    go (plotly.graph_objects), px (plotly.express), json,
    execute_query(sql, db=None, max_rows=None) → list[dict].

    CRITICAL RULES:
    3. ALWAYS prefix tables: pwc_macro_staging_schema.<table_name>
    4. Use the pre-injected execute_query(sql) to run SQL — it returns list[dict].
       Alternatively use pd.read_sql("SELECT ...", conn) for DataFrames.
       Do NOT redefine execute_query yourself.
    5. Set `result = <value>` to return data. DataFrames are auto-converted.
    6. On error: read the FULL 'error' and 'traceback' fields carefully, diagnose
       the root cause, fix your code, and call this tool again with corrected code.
       You may retry up to 3 times total — each retry must have a meaningful fix.

    RETURNS: JSON with 'status' and 'result'. On error: 'error' and 'traceback'.
    """
    sandbox = PythonSandbox()
    result = sandbox.execute(code, timeout_seconds)
    return _truncate_tool_output("run_sql_python", json.dumps(result, default=str))


# ─────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────

def get_all_tools() -> list:
    """Return all tools for the traversal agent, ordered by KPI-first priority."""
    return [
        get_kpi,
        get_node,
        find_relevant,
        traverse_graph,
        run_sql_python,
        run_python,
        run_cypher,
    ]
