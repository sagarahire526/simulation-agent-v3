"""
Python Sandbox tool for executing computation code safely.
Used by the Traversal Agent and Response Agent for calculations.
"""
from __future__ import annotations

import ast
import re
import time
import logging
import traceback
from typing import Any
from io import StringIO
import contextlib
import math
import json
import statistics

import psycopg2
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import concurrent.futures
import config

logger = logging.getLogger(__name__)

# Allowed built-in modules for the sandbox
SAFE_MODULES = {
    "math": math,
    "json": json,
    "statistics": statistics,
    "numpy": np,
    "pandas": pd,
    "collections": __import__("collections"),
    "datetime": __import__("datetime"),
    "itertools": __import__("itertools"),
    "functools": __import__("functools"),
}

# Blocked built-in functions
BLOCKED_BUILTINS = {
    "exec", "eval", "compile", "open",
    "breakpoint", "exit", "quit",
}


def _safe_import(name, *args, **kwargs):
    """Only allow importing whitelisted modules."""
    top_level = name.split(".")[0]
    if top_level not in SAFE_MODULES and top_level not in ("collections", "datetime", "itertools", "functools"):
        raise ImportError(f"Import of '{name}' is not allowed in sandbox.")
    return __import__(name, *args, **kwargs)


def _validate_code(code: str) -> tuple[bool, str]:
    """
    Static analysis to reject dangerous code patterns.
    Returns (is_safe, reason).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        # Block imports except whitelisted
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[0]
            elif isinstance(node, ast.Import):
                module = node.names[0].name.split(".")[0]

            if module not in SAFE_MODULES and module not in ("collections", "datetime", "itertools", "functools"):
                return False, f"Import of '{module}' is not allowed in sandbox."

        # Block attribute access to dunder methods (except __init__, __str__, __repr__)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in ("__init__", "__str__", "__repr__", "__len__"):
                return False, f"Access to '{node.attr}' is not allowed."

    return True, "OK"


def _strip_markdown_fences(code: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap around code.

    Handles ```python ... ``` and ``` ... ```.
    """
    lines = code.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]  # remove opening fence
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]  # remove closing fence
    return "\n".join(lines)


def _fix_literal_escapes(code: str) -> str:
    """Fix LLM sending literal backslash-n instead of real newlines.

    When the entire code appears on one line with literal '\\n' sequences,
    convert them to real newlines so the code can be parsed properly.
    """
    # If code has no real newlines but contains literal \n sequences → convert
    if "\n" not in code and "\\n" in code:
        code = code.replace("\\n", "\n")
        code = code.replace("\\t", "\t")
    return code


def _sanitize_continuations(code: str) -> str:
    """Fix backslash line-continuation errors that LLMs generate.

    Strategy: try to compile first — if the code is valid, return it untouched.
    Only when there is an actual 'line continuation' SyntaxError do we apply
    targeted fixes (on the offending lines only).  This avoids the previous
    approach of global regex that corrupted content inside triple-quoted
    strings (causing 'unterminated triple-quoted string literal' errors).
    """
    # Fast path: code already compiles — don't touch it
    try:
        compile(code, "<sanitize>", "exec")
        return code
    except SyntaxError as e:
        if "line continuation" not in str(e):
            return code  # different error — not our problem

    # Targeted fix loop: let Python's parser tell us which line is broken,
    # then remove the stray backslash(es) on that line only.
    for _ in range(10):
        try:
            compile(code, "<sanitize>", "exec")
            break  # compiles clean — done
        except SyntaxError as e:
            if "line continuation" not in str(e):
                break  # different error — not our problem
            if e.lineno is None:
                break
            lines = code.splitlines()
            if e.lineno > len(lines):
                break
            # Remove backslashes from the offending line
            lines[e.lineno - 1] = lines[e.lineno - 1].replace("\\", "")
            code = "\n".join(lines)
    return code


def execute_python(code: str, context: dict[str, Any] | None = None) -> dict:
    """
    Execute Python code in a restricted sandbox.

    Args:
        code: Python code string
        context: Variables to inject into the execution namespace

    Returns:
        dict with status, output (stdout), result (last expression), error
    """
    # Clean up LLM-generated code: literal escapes, markdown fences, trailing whitespace, continuations
    code = _fix_literal_escapes(code)
    code = _strip_markdown_fences(code)
    code = "\n".join(line.rstrip() for line in code.splitlines())
    code = _sanitize_continuations(code)

    is_safe, reason = _validate_code(code)
    if not is_safe:
        return {
            "status": "error",
            "error": f"Code validation failed: {reason}",
            "output": "",
            "result": None,
        }

    # Build restricted globals
    safe_builtins = {
        k: v for k, v in __builtins__.__dict__.items()
        if k not in BLOCKED_BUILTINS
    } if hasattr(__builtins__, "__dict__") else {
        k: v for k, v in __builtins__.items()
        if k not in BLOCKED_BUILTINS
    }

    # Allow imports but only for whitelisted modules
    safe_builtins["__import__"] = _safe_import

    namespace = {
        "__builtins__": safe_builtins,
        **SAFE_MODULES,
        # Common aliases — pre-injected so LLM doesn't need import statements
        "np": np,
        "pd": pd,
    }

    # Inject context variables (e.g., data from previous steps)
    if context:
        namespace.update(context)

    # Capture stdout
    stdout_capture = StringIO()
    start = time.perf_counter()

    try:
        # If last line is a bare expression (not assignment), auto-capture it as result
        lines = code.strip().splitlines()
        last_line = lines[-1].strip() if lines else ""
        auto_capture = False
        if last_line and not any(last_line.startswith(k) for k in ("result", "#", "print", "import", "from", "if ", "for ", "while ", "def ", "class ", "return", "try", "except", "with ")):
            try:
                ast.parse(last_line, mode="eval")
                auto_capture = True
            except SyntaxError:
                pass

        with contextlib.redirect_stdout(stdout_capture):
            exec(code, namespace)

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Try to extract a 'result' variable if set by the code
        result = namespace.get("result", None)

        # Auto-capture: if result was never set, evaluate the last expression
        if result is None and auto_capture:
            try:
                result = eval(last_line, namespace)  # noqa: S307
            except Exception:
                pass

        # Last resort: use stdout if nothing else captured
        if result is None and stdout_capture.getvalue().strip():
            result = stdout_capture.getvalue().strip()

        return {
            "status": "success",
            "output": stdout_capture.getvalue(),
            "result": result,
            "elapsed_ms": round(elapsed_ms, 2),
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "output": stdout_capture.getvalue(),
            "result": None,
            "elapsed_ms": round(elapsed_ms, 2),
        }


# ── PostgreSQL-backed sandbox ────────────────────────────────────────────────

class PythonSandbox:
    """
    PostgreSQL-backed execution sandbox.

    Provides `conn` (psycopg2, read-only), `pd`, `np`, `go`, `px`, `json`
    in the execution namespace. User code sets a `result` dict to return data.
    """

    def __init__(self):
        self.conn = None
        self.session_vars = {}
        self._connect()

    def _connect(self):
        """Lazily connect to Postgres. Gracefully handles missing DB."""
        if self.conn is not None:
            return
        try:
            self.conn = psycopg2.connect(
                host=config.PG_HOST,
                port=config.PG_PORT,
                database=config.PG_DATABASE,
                user=config.PG_USER,
                password=config.PG_PASSWORD,
                options="-c default_transaction_read_only=on",
            )
            self.conn.autocommit = True
        except Exception as e:
            print(f"⚠ Postgres not available: {e}")
            self.conn = None

    # DML/DDL keywords that must never be executed
    _BLOCKED_SQL_KEYWORDS = {
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
        "CREATE", "GRANT", "REVOKE", "MERGE", "REPLACE",
    }

    def _is_raw_sql(self, code: str) -> bool:
        """Detect if code is raw SQL rather than Python."""
        first_line = code.strip().split("\n")[0].strip().rstrip(";").upper()
        sql_starts = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WITH ", "EXPLAIN ")
        return first_line.startswith(sql_starts)

    def _validate_sql(self, code: str) -> tuple[bool, str]:
        """
        Scan Python code for embedded SQL strings containing DML/DDL.
        Returns (is_safe, reason).
        """
        # Extract all string literals that look like SQL
        # Match quoted strings (single, double, triple-quoted)
        sql_patterns = re.findall(
            r'(?:"""(.*?)"""|\'\'\'(.*?)\'\'\'|"(.*?)"|\'(.*?)\')',
            code, re.DOTALL,
        )
        for groups in sql_patterns:
            for sql_str in groups:
                if not sql_str:
                    continue
                # Normalize and check first meaningful word
                stripped = sql_str.strip().upper()
                first_word = stripped.split()[0] if stripped.split() else ""
                if first_word in self._BLOCKED_SQL_KEYWORDS:
                    return False, (
                        f"DML/DDL operation '{first_word}' is blocked. "
                        "Only SELECT/WITH queries are allowed."
                    )
        return True, "OK"

    def execute(self, code: str, timeout_seconds: int = 30) -> dict:
        if self.conn is None:
            self._connect()

        # Clean up LLM-generated code: literal escapes, markdown fences, trailing whitespace, continuations
        code = _fix_literal_escapes(code)
        code = _strip_markdown_fences(code)
        code = "\n".join(line.rstrip() for line in code.splitlines())
        code = _sanitize_continuations(code)

        # Block DML/DDL before any execution
        is_safe, reason = self._validate_sql(code)
        if not is_safe:
            return {"status": "error", "error": reason}

        # Auto-wrap raw SQL in pd.read_sql() so exec() doesn't choke on it
        if self._is_raw_sql(code):
            # Block raw DML/DDL
            first_word = code.strip().split()[0].rstrip(";").upper()
            if first_word in self._BLOCKED_SQL_KEYWORDS:
                return {
                    "status": "error",
                    "error": f"DML/DDL operation '{first_word}' is blocked. Only SELECT/WITH queries are allowed.",
                }
            escaped = code.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
            code = f'result = pd.read_sql("""{escaped}""", conn).to_dict(orient="records")'

        def _execute_query(sql, params=None, db=None, max_rows=None):
            """Helper: run SQL and return list[dict] (not a DataFrame)."""
            try:
                normalized = " ".join(sql.split())
                where_blocks = re.findall(
                    r"\bWHERE\b(.+?)(?=\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|\bHAVING\b|\bUNION\b|\)\s*(?:,|\bAS\b|$)|;|$)",
                    normalized,
                    flags=re.IGNORECASE,
                )
                where_blocks = [w.strip() for w in where_blocks] or ["(no WHERE clause)"]
                for i, block in enumerate(where_blocks, 1):
                    label = f"WHERE #{i}" if len(where_blocks) > 1 else "filters"
                    print(
                        f"\033[96m🔎 execute_query — {label}:\033[0m {block}",
                        flush=True,
                    )
                    logger.info("execute_query %s: %s", label, block)
                if params:
                    print(f"\033[96m🔎 execute_query — params:\033[0m {params}", flush=True)
                    logger.info("execute_query params: %s", params)
            except Exception:
                pass

            df = pd.read_sql(sql, self.conn, params=params)
            if max_rows is not None:
                df = df.head(max_rows)
            return df.to_dict(orient="records")

        def _run_construction_plan_forecast(
            target_sites,
            window_days=60,
            prereq_threshold=0.80,
            project_type="NTM",
            filters=None,
            split_on_gate=None,
            mobilization_buffer_days=10,
            pull_forward_lookup_days=None,
            include_crew_analysis=False,
            sites_per_crew_per_week=None,
            node_id="cpf-001-construction-plan-forecast",
        ):
            """Pre-injected helper for the Construction Plan Forecast KPI.

            Pulls `kpi_python_function` (full `build_plan` source) and
            `kpi_sla_dag` (per-project-type DAG, JSON) from Neo4j, exec's
            the function in an isolated namespace, then calls `build_plan`
            with the same `execute_query` this sandbox exposes. Returns the
            full plan dict — summary, weekly_buckets, committed_sites,
            pull_forward_sites, per_gc_weekly_demand, capacity, config (and
            crew_gap when include_crew_analysis=True).

            Use this INSTEAD of pasting `kpi_python_function` source into
            the sandbox. Pasting a ~40 KB function body has repeatedly
            produced indentation / null-byte / return→result corruption
            from the LLM render path. The helper sidesteps that entirely.
            """
            from tools.neo4j_tool import Neo4jTool
            out = Neo4jTool().run_cypher_safe(
                "MATCH (n:BKGNode {node_id: $nid}) "
                "RETURN coalesce(n.kpi_python_function, '') AS fn, "
                "       coalesce(n.kpi_sla_dag, '{}')      AS dag",
                {"nid": node_id},
            )
            records = (out.get("records") or out.get("results") or []) if isinstance(out, dict) else []
            if not records:
                raise RuntimeError(
                    f"Construction Plan Forecast node '{node_id}' not found in Neo4j. "
                    "Load the cypher append via: python3 -m scripts.load_cpf_node"
                )
            row = records[0]
            fn_src = row.get("fn") or ""
            dag_str = row.get("dag") or "{}"
            if len(fn_src) < 1000:
                raise RuntimeError(
                    f"kpi_python_function on '{node_id}' is suspiciously short ({len(fn_src)} chars). "
                    "Node may have loaded partially — re-run scripts.load_cpf_node --force."
                )
            sla_dag = json.loads(dag_str)
            local_ns = {}
            exec(fn_src, local_ns)
            build_plan = local_ns.get("build_plan")
            if build_plan is None:
                raise RuntimeError(
                    "build_plan was not defined after exec — the node may have a broken function body."
                )
            return build_plan(
                target_sites=target_sites,
                window_days=window_days,
                prereq_threshold=prereq_threshold,
                project_type=project_type,
                sla_dag=sla_dag,
                execute_query=_execute_query,
                filters=filters,
                split_on_gate=split_on_gate,
                mobilization_buffer_days=mobilization_buffer_days,
                pull_forward_lookup_days=pull_forward_lookup_days,
                include_crew_analysis=include_crew_analysis,
                sites_per_crew_per_week=sites_per_crew_per_week,
            )

        def _fetch_node_source(node_id, field):
            """Fetch a stored function string for a node from Neo4j (or '' if absent)."""
            from tools.neo4j_tool import Neo4jTool
            out = Neo4jTool().run_cypher_safe(
                f"MATCH (n:BKGNode {{node_id: $nid}}) RETURN coalesce(n.{field}, '') AS fn",
                {"nid": node_id},
            )
            records = (out.get("records") or out.get("results") or []) if isinstance(out, dict) else []
            return (records[0].get("fn") if records else "") or ""

        def _pick_callable(ns, prefixes):
            """Return the first top-level user function in `ns` whose name starts with
            one of `prefixes`; fall back to the first non-dunder user function."""
            import types
            funcs = {k: v for k, v in ns.items()
                     if isinstance(v, types.FunctionType) and not k.startswith("__")}
            for p in prefixes:
                for k, v in funcs.items():
                    if k.startswith(p):
                        return v
            return next(iter(funcs.values())) if funcs else None

        def run_node(node_id, filter=None, group_by=None):
            """Deterministically execute a KPI/core node's stored function with the
            SAME execute_query this sandbox exposes — NO LLM, NO GROUP BY stripping.

            Fetches kpi_python_function (or map_python_function for core nodes),
            exec's it, locates the get_* callable, merges `group_by` into the filter
            dict, and calls fn(execute_query, filters=<merged>). Returns list[dict].
            """
            fn_src = _fetch_node_source(node_id, "kpi_python_function")
            if not fn_src:
                fn_src = _fetch_node_source(node_id, "map_python_function")
            if not fn_src:
                raise RuntimeError(f"Node '{node_id}' has no kpi_python_function/map_python_function.")
            local_ns = {}
            exec(fn_src, local_ns)
            fn = _pick_callable(local_ns, ("get_",))
            if fn is None:
                raise RuntimeError(f"No get_* callable found in node '{node_id}' function body.")
            merged = dict(filter or {})
            if group_by is not None:
                merged["group_by"] = group_by
            return fn(_execute_query, merged)

        def run_transform(node_id, *args, **kwargs):
            """Execute a pure-transform node (e.g. a predictor) — calls the node's
            stored function with the given args/kwargs, WITHOUT execute_query."""
            fn_src = _fetch_node_source(node_id, "kpi_python_function")
            if not fn_src:
                raise RuntimeError(f"Transform node '{node_id}' has no kpi_python_function.")
            local_ns = {}
            exec(fn_src, local_ns)
            fn = _pick_callable(local_ns, ("predict_", "transform_", "compute_"))
            if fn is None:
                raise RuntimeError(f"No transform callable found in node '{node_id}' function body.")
            return fn(*args, **kwargs)

        def run_scenario(scenario_id, filter=None, group_by=None):
            """Run a scenario node's deterministic orchestrator (scn_python_function),
            passing the run_node + run_transform helpers so it can chain the
            contributing nodes with NO LLM in the loop."""
            fn_src = _fetch_node_source(scenario_id, "scn_python_function")
            if not fn_src:
                raise RuntimeError(f"Scenario '{scenario_id}' has no scn_python_function.")
            local_ns = {}
            exec(fn_src, local_ns)
            fn = _pick_callable(local_ns, ("run_", "scenario_"))
            if fn is None:
                raise RuntimeError(f"No run_* orchestrator found in scenario '{scenario_id}'.")
            # Expose the node runners AND the construction-plan-forecast wrapper as
            # globals so an orchestrator can call any of them directly (cpf-001 can't
            # go through run_node — build_plan needs the SLA DAG + a bespoke signature).
            local_ns["run_node"] = run_node
            local_ns["run_transform"] = run_transform
            local_ns["run_construction_plan_forecast"] = _run_construction_plan_forecast
            return fn(run_node, run_transform, filter=filter, group_by=group_by)

        namespace = {
            "conn": self.conn,
            "pd": pd,
            "np": np,
            "go": go,
            "px": px,
            "json": json,
            "session": self.session_vars,
            "execute_query": _execute_query,
            "run_construction_plan_forecast": _run_construction_plan_forecast,
            "run_node": run_node,
            "run_transform": run_transform,
            "run_scenario": run_scenario,
            "result": None,
        }

        # Detect if last line is a bare expression (auto-capture as result)
        lines = code.strip().splitlines()
        last_line = lines[-1].strip() if lines else ""
        auto_capture = False
        if last_line and not any(last_line.startswith(k) for k in ("result", "#", "print", "import", "from", "if ", "for ", "while ", "def ", "class ", "return", "try", "except", "with ")):
            try:
                ast.parse(last_line, mode="eval")
                auto_capture = True
            except SyntaxError:
                pass

        def _run():
            exec(code, namespace)  # noqa: S102
            return namespace

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run)
                try:
                    result_ns = future.result(timeout=timeout_seconds)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"Execution timed out after {timeout_seconds}s"
                    )

            if "session" in result_ns:
                self.session_vars = result_ns["session"]

            result = result_ns.get("result", None)

            # Auto-capture: if result was never set, evaluate the last expression
            if result is None and auto_capture:
                try:
                    result = eval(last_line, result_ns)  # noqa: S307
                except Exception:
                    pass

            # Handle result being a DataFrame, list, or other non-dict type
            if isinstance(result, pd.DataFrame):
                result = result.to_dict(orient="records")
            elif isinstance(result, dict):
                for key, val in list(result.items()):
                    if isinstance(val, pd.DataFrame):
                        result[key] = val.to_dict(orient="records")
            elif result is None:
                result = {}

            # Detect empty results and flag for the agent to re-examine filters
            response = {"status": "success", "result": result}
            is_empty = (
                (isinstance(result, list) and len(result) == 0)
                or (isinstance(result, dict) and all(
                    (isinstance(v, list) and len(v) == 0) for v in result.values()
                ))
            )
            if is_empty:
                response["empty_result_warning"] = (
                    "Query returned 0 rows. This usually means WHERE clause "
                    "filters (IS NOT NULL, IS NULL, specific value checks) are "
                    "too restrictive. Re-examine your query: remove IS NOT NULL "
                    "/ IS NULL conditions and non-essential filters, then retry."
                )
            return response

        except Exception as e:
            # print(f"status is ERROR and error message is {str(e)} and traceback is {traceback.format_exc()}")
            return {
                "status": "error",
                "error": str(e),
            }

    def close(self):
        if self.conn:
            self.conn.close()
