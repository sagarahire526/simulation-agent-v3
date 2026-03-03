"""
Sandbox Service — business logic layer for the PostgreSQL Python sandbox.

Manages the PythonSandbox singleton and exposes clean methods
consumed by the API endpoint layer.
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.python_sandbox import PythonSandbox

logger = logging.getLogger(__name__)

_instance: Optional[PythonSandbox] = None


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
