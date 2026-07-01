"""
JSON utilities — a single hardened serializer shared by every write path.

Two output paths carry the same graph state to the client:
  • the DB persistence path (db_service.update_query_complete)
  • the live SSE stream path (sse_simulate._event_generator)

They MUST serialize identically. If they diverge — as they did once, when the
SSE path used a naive ``json.dumps`` — a payload that the DB stores cleanly can
become invalid/undeliverable over SSE (e.g. ``NaN``/``Infinity`` tokens that
``JSON.parse`` rejects, or non-JSON-native types that raise ``TypeError``),
forcing the frontend to refresh and re-fetch from the DB. Routing both paths
through ``safe_dumps`` here removes that class of bug permanently.
"""
from __future__ import annotations

import json
import math


def json_safe(obj):
    """
    Recursively replace JSON-incompatible scalars (NaN, ±Infinity, pandas NaT,
    numpy scalars, pandas Timestamps) with JSON-safe equivalents.

    Standard JSON has no NaN/Infinity tokens. Python's ``json.dumps`` emits them
    by default (``allow_nan=True``), which:
      - PostgreSQL JSONB rejects (`invalid input syntax for type json`), and
      - browsers reject in ``JSON.parse`` (invalid JSON) once sent over SSE.
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    # Lazy pandas/numpy handling — only imported if needed
    try:
        import pandas as pd
        if obj is pd.NaT or (pd.api.types.is_scalar(obj) and pd.isna(obj)):
            return None
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
    except ImportError:
        pass
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            val = obj.item()
            return val if not (isinstance(val, float) and not math.isfinite(val)) else None
    except ImportError:
        pass
    return obj


def safe_dumps(obj) -> str:
    """
    ``json.dumps`` hardened for both PostgreSQL JSONB and browser ``JSON.parse``.

    - ``json_safe`` sanitises NaN/NaT/Infinity and numpy/pandas scalars.
    - ``allow_nan=False`` guarantees no NaN/Infinity tokens ever reach output.
    - ``default=str`` stringifies any remaining non-JSON-native type
      (datetime, Decimal, UUID, …) instead of raising ``TypeError``.
    """
    return json.dumps(json_safe(obj), allow_nan=False, default=str)
