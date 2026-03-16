"""
LangChain tool wrappers for the autonomous Traversal Agent.

Wraps existing tools (neo4j_tool, bkg_tool, python_sandbox) as
@tool functions that the ReAct agent can call.

Tools are ordered by recommended usage sequence:
  1. find_relevant  — discover relevant KG nodes (start here)
  2. get_node       — inspect a specific node's full details
  3. traverse_graph — walk relationships from a node
  4. get_kpi        — get KPI formula, logic, and computation details
  5. get_table_schema — discover database tables and columns
  6. run_cypher     — custom Neo4j queries
  7. run_sql_python — query PostgreSQL with Python
  8. run_python     — sandboxed calculations
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
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# BKG High-Level Tools
# ─────────────────────────────────────────────

@tool
def get_node(node_id: str) -> str:
    """Fetch a single node from the Knowledge Graph by its node_id.

    USE WHEN: You know the exact node_id you want to inspect and need its full
    properties plus all incoming and outgoing relationships.

    DETAILS:
    - Returns ALL properties on the node (core props, map_* for database mappings,
      kpi_* for KPI computation details).
    - Returns outgoing and incoming relationships with target/source node_id,
      label, entity_type, and relationship type.
    - Supports aliases: 'GC' → general_contractor, 'BOM' → bill_of_materials, etc.

    SEQUENCE: Use find_relevant FIRST to discover node_ids, then get_node for details.
    """
    result = _get_bkg().query({"mode": "get_node", "node_id": node_id})
    return json.dumps(result, default=str)


@tool
def find_relevant(question: str) -> str:
    """Keyword search across all BKGNode nodes in the Knowledge Graph.

    USE WHEN: You are starting a new exploration and need to discover which nodes
    relate to a question. This should be your FIRST tool call for any new query.

    SEARCHES ACROSS: node_id, name, label, definition, nl_description, entity_type,
    kpi_name, kpi_description.

    RETURNS: Up to 10 nodes ranked by relevance score, each with:
    - node_id, name, label, entity_type, definition (truncated)
    - For KPI nodes: kpi_name, kpi_description
    - For core nodes with mappings: map_table_name
    - Neighbor preview (up to 5 connected nodes)

    NEXT STEPS after find_relevant:
    - Use get_node(node_id) to inspect a specific node in detail.
    - Use traverse_graph(node_id) to explore its neighborhood.
    - Use get_kpi(node_id) if entity_type is 'kpi' and you need formula details.
    """
    result = _get_bkg().query({"mode": "find_relevant", "question": question})
    return json.dumps(result, default=str)


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
    return json.dumps(result, default=str)


@tool
def get_kpi(node_id: str) -> str:
    """Get detailed KPI computation information for a KPI node.

    USE WHEN: You found a KPI node (entity_type='kpi') and need to understand:
    - What it measures (kpi_description, kpi_formula_description)
    - How to compute it (kpi_business_logic, kpi_python_function)
    - What data it needs (kpi_source_tables, kpi_source_columns, kpi_dimensions)
    - How to filter it (kpi_filters)
    - What it outputs (kpi_output_schema)
    - Its function contract (kpi_contract)

    ALSO: If the node_id is a core/context node (not a KPI), this tool will return
    all KPI nodes that reference or compute from it — useful for discovering which
    KPIs relate to a business entity.

    SEQUENCE: find_relevant → get_kpi(kpi_node_id) → use kpi_python_function
    or kpi_source_tables to write your SQL/Python query.
    """
    result = _get_bkg().query({"mode": "get_kpi", "node_id": node_id})
    return json.dumps(result, default=str)


@tool
def get_table_schema(table_name: str = "") -> str:
    """Get database table mappings from the Knowledge Graph.

    USE WHEN: You need to understand what database tables exist and how to query
    them BEFORE writing any SQL. Never guess table or column names.

    USAGE PATTERN:
    1. Call get_table_schema("") (empty string) → lists ALL available tables with
       their mapped nodes, key columns, and label columns.
    2. Call get_table_schema("exact_table_name") → returns detailed mapping for
       that table: map_key_column, map_label_column, map_sql_template,
       map_python_function, and map_contract for each node using that table.

    IMPORTANT: The map_sql_template and map_python_function fields contain
    ready-to-use SQL and Python code for querying the table. Use these as
    templates instead of writing queries from scratch.

    NEXT STEP: Use the discovered table/column names in run_sql_python.
    Always prefix tables with the correct schema (e.g., pwc_macro_staging_schema.<table>).
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
    return json.dumps(result, default=str)


@tool
def run_sql_python(code: str, timeout_seconds: int = 30) -> str:
    """Execute Python code with access to a read-only PostgreSQL database connection.

    USE WHEN: You need to query the PostgreSQL database for actual operational data.
    The Neo4j Knowledge Graph describes the data MODEL; this tool queries the
    actual DATA.

    PRE-IMPORTED: conn (psycopg2 read-only), pd (pandas), np (numpy),
    go (plotly.graph_objects), px (plotly.express), json.

    CRITICAL RULES:
    1. ALWAYS call get_table_schema("") first to discover available tables.
    2. ALWAYS call get_table_schema("table_name") to get exact column names.
    3. ALWAYS prefix tables: pwc_macro_staging_schema.<table_name>
    4. Use pd.read_sql("SELECT ...", conn) — never raw SQL.
    5. Set `result = <value>` to return data. DataFrames are auto-converted.
    6. On error: read the FULL 'error' and 'traceback' fields carefully, diagnose
       the root cause, fix your code, and call this tool again with corrected code.
       You may retry up to 3 times total — each retry must have a meaningful fix.

    RETURNS: JSON with 'status' and 'result'. On error: 'error' and 'traceback'.
    """
    sandbox = PythonSandbox()
    result = sandbox.execute(code, timeout_seconds)
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────

def get_all_tools() -> list:
    """Return all tools for the traversal agent, ordered by recommended usage."""
    return [
        find_relevant,
        get_node,
        traverse_graph,
        get_kpi,
        get_table_schema,
        run_cypher,
        run_sql_python,
        run_python,
    ]
