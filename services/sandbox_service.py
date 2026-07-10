"""
Sandbox Service — business logic layer for the PostgreSQL Python sandbox.

Manages the PythonSandbox singleton and exposes clean methods
consumed by the API endpoint layer.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from tools.python_sandbox import PythonSandbox

logger = logging.getLogger(__name__)

_instance: Optional[PythonSandbox] = None

_NODE_MODES = ("node", "scenario", "transform")


def _get_sandbox() -> PythonSandbox:
    global _instance
    if _instance is None:
        _instance = PythonSandbox()
    return _instance


def health() -> dict:
    """Return PostgreSQL connectivity status."""
    try:
        sb = _get_sandbox()
        if sb.conn is not None:
            return {"status": "connected"}
        return {"status": "unavailable"}
    except Exception as e:
        logger.warning("Postgres health check failed: %s", e)
        return {"status": "unavailable", "error": str(e)}


def execute(code: str, timeout_seconds: int = 30) -> dict:
    """
    Execute Python code in the PostgreSQL-backed sandbox.

    Raises ValueError on empty code so the endpoint can return HTTP 400.
    """
    if not code.strip():
        raise ValueError("Code cannot be empty")
    return _get_sandbox().execute(code, timeout_seconds)


def _build_node_code(
    mode: str,
    node_id: str,
    filters: dict[str, Any],
    group_by: Optional[str],
    args: list,
    kwargs: dict[str, Any],
) -> str:
    """Build the sandbox code that invokes a stored node function via the pre-injected
    run_node / run_scenario / run_transform helpers. Params are passed as JSON DATA
    (never interpolated as code), so this is not a code-injection surface."""
    if mode == "node":
        payload = json.dumps({"node_id": node_id, "filter": filters, "group_by": group_by})
        call = "run_node(_p['node_id'], _p['filter'], _p['group_by'])"
    elif mode == "scenario":
        payload = json.dumps({"scenario_id": node_id, "filter": filters, "group_by": group_by})
        call = "run_scenario(_p['scenario_id'], _p['filter'], _p['group_by'])"
    else:  # transform
        payload = json.dumps({"node_id": node_id, "args": args, "kwargs": kwargs})
        call = "run_transform(_p['node_id'], *_p['args'], **_p['kwargs'])"
    return ("import json as _j\n"
            f"_p = _j.loads({payload!r})\n"
            f"result = {call}")


def run_bkg_node(
    mode: str,
    node_id: str,
    *,
    filters: Optional[dict[str, Any]] = None,
    group_by: Optional[str] = None,
    args: Optional[list] = None,
    kwargs: Optional[dict[str, Any]] = None,
    timeout_seconds: int = 120,
) -> dict:
    """Execute a stored BKG node function in isolation against the read-only Postgres,
    using the SAME sandbox helpers the agent uses — so results match production.

      mode='node'       -> run_node(node_id, filters, group_by)      [kpi/core get_*]
      mode='scenario'   -> run_scenario(node_id, filters, group_by)
      mode='transform'  -> run_transform(node_id, *args, **kwargs)   [pure predictor]

    Returns the sandbox execute() dict: {status, result, output, error}. Raises
    ValueError (→ HTTP 400) on bad input.
    """
    if not node_id or not node_id.strip():
        raise ValueError("node_id is required")
    if mode not in _NODE_MODES:
        raise ValueError(f"invalid mode '{mode}' — expected one of {_NODE_MODES}")
    code = _build_node_code(
        mode, node_id.strip(), filters or {}, group_by, args or [], kwargs or {}
    )
    return _get_sandbox().execute(code, timeout_seconds)
