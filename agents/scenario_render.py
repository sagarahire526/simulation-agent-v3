"""
Scenario result rendering.

Deterministic scenario nodes (esp. the cpf-001–wrapping ones) return planning-level
aggregates. ``render_scenario_findings`` renders ANY scenario result to markdown generically,
at the response boundary (agents/response.py) and OUT of the planner (which just executes the
scenario).
"""
from __future__ import annotations

def _union_columns(rows: list[dict]) -> list[str]:
    """First-seen-order union of keys across a list of row dicts."""
    cols: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    return cols


def _md_table(rows: list[dict], columns: list[str]) -> str:
    """Render row dicts as a GitHub markdown table for the given columns."""
    if not rows:
        return "_(no rows)_"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(str(r.get(c, "")) for c in columns) + " |" for r in rows]
    return "\n".join([header, sep, *body])


def _render_block(key: str, val, out: list[str], level: int) -> None:
    """Recursively render one key/value into markdown lines. Generic over any shape:
      - list of dicts        -> markdown table
      - list of scalars      -> inline comma list
      - flat dict (scalars)  -> key: value bullet block under a heading
      - nested dict          -> heading, then recurse each sub-key
      - scalar               -> bold key: value line
    """
    heading = "#" * min(max(level, 1), 6)
    if isinstance(val, list):
        if val and all(isinstance(x, dict) for x in val):
            out.append(f"{heading} {key} — {len(val)} row(s)")
            out.append(_md_table(val, _union_columns(val)))
            out.append("")
        else:
            preview = ", ".join(str(x) for x in val) if val else "(empty)"
            out.append(f"**{key}** ({len(val)}): {preview}")
    elif isinstance(val, dict):
        if not val:
            out.append(f"**{key}**: (empty)")
        elif all(not isinstance(v, (dict, list)) for v in val.values()):
            out.append(f"{heading} {key}")
            for k, v in val.items():
                out.append(f"- {k}: {v}")
            out.append("")
        else:
            out.append(f"{heading} {key}")
            for k, v in val.items():
                _render_block(k, v, out, level + 1)
    else:
        out.append(f"**{key}**: {val}")


def render_scenario_findings(label: str, resolved: dict, result) -> str:
    """Render ANY scenario's result as verbatim markdown so the response agent sees the real
    numbers. Fully generic (recurses arbitrarily nested dicts/lists) — makes no assumptions
    about a scenario's output shape. Pass an already-lean result to bound the LLM prompt.

    The scenario is already fully computed by the graph nodes; this is render-only."""
    lines = [
        f"DETERMINISTIC SCENARIO RESULT — '{label}'. Already computed by the graph "
        f"nodes (no LLM grouping/filtering). RENDER THE DATA BELOW VERBATIM — do NOT "
        f"recompute, summarise away, drop rows, or claim any data is missing.",
        "",
        f"Resolved scope: {resolved or {}}",
        "",
    ]
    if not isinstance(result, dict):
        lines.append(str(result))
        return "\n".join(lines)

    for key, val in result.items():
        _render_block(key, val, lines, level=4)
    return "\n".join(lines)
