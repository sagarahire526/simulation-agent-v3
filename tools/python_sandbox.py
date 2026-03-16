"""
Python Sandbox tool for executing computation code safely.
Used by the Traversal Agent and Response Agent for calculations.
"""
from __future__ import annotations

import ast
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


def execute_python(code: str, context: dict[str, Any] | None = None) -> dict:
    """
    Execute Python code in a restricted sandbox.

    Args:
        code: Python code string
        context: Variables to inject into the execution namespace

    Returns:
        dict with status, output (stdout), result (last expression), error
    """
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

    def _is_raw_sql(self, code: str) -> bool:
        """Detect if code is raw SQL rather than Python."""
        first_line = code.strip().split("\n")[0].strip().rstrip(";").upper()
        sql_starts = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WITH ", "EXPLAIN ")
        return first_line.startswith(sql_starts)

    def execute(self, code: str, timeout_seconds: int = 30) -> dict:
        if self.conn is None:
            self._connect()

        # Auto-wrap raw SQL in pd.read_sql() so exec() doesn't choke on it
        if self._is_raw_sql(code):
            escaped = code.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
            code = f'result = pd.read_sql("""{escaped}""", conn).to_dict(orient="records")'

        def _execute_query(sql, db=None, max_rows=None):
            """Helper: run SQL and return list[dict] (not a DataFrame)."""
            df = pd.read_sql(sql, self.conn)
            if max_rows is not None:
                df = df.head(max_rows)
            return df.to_dict(orient="records")

        namespace = {
            "conn": self.conn,
            "pd": pd,
            "np": np,
            "go": go,
            "px": px,
            "json": json,
            "session": self.session_vars,
            "execute_query": _execute_query,
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
            return {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    def close(self):
        if self.conn:
            self.conn.close()
