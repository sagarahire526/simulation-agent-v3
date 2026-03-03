"""
BKG Service — business logic layer for the Business Knowledge Graph.

Manages the BKGTool singleton and exposes clean methods
consumed by the API endpoint layer.
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.bkg_tool import BKGTool

logger = logging.getLogger(__name__)

_instance: Optional[BKGTool] = None


def _get_tool() -> BKGTool:
    global _instance
    if _instance is None:
        _instance = BKGTool()
    return _instance


def health() -> dict:
    """Return Neo4j connectivity status."""
    try:
        tool = _get_tool()
        return {"status": "connected", "node_count": len(tool.nodes)}
    except Exception as e:
        logger.warning("Neo4j health check failed: %s", e)
        return {"status": "unavailable", "error": str(e)}


def query(request: dict) -> dict:
    """
    Route a BKG query request to the BKGTool.

    Raises ValueError when the tool returns an error key so the endpoint
    can map it to the appropriate HTTP status code.
    """
    result = _get_tool().query(request)
    if "error" in result:
        raise ValueError(result["error"])
    return result


def get_schema(table_name: Optional[str] = None) -> dict:
    """Return the BKG schema overview, optionally filtered by table name."""
    payload: dict = {"mode": "schema"}
    if table_name:
        payload["table_name"] = table_name
    return _get_tool().query(payload)
