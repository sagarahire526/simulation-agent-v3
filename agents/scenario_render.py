"""
Scenario result rendering + response-facing trimming.

Deterministic scenario nodes (esp. the cpf-001–wrapping ones) return rich results that can
embed per-site object lists for the UI. Those are great for the client but explode the
RESPONSE AGENT's prompt on large real datasets. This module keeps two concerns together and
OUT of the planner (which should just execute the scenario):

  - ``lean_scenario_result``  — trim a result to planning-level aggregates for the LLM,
                                dropping heavy per-site object/id lists (replaced by counts)
                                and capping long lists. The FULL result is kept elsewhere.
  - ``render_scenario_findings`` — render ANY scenario result to markdown, generically.

The trimming is applied at the response boundary (agents/response.py), not in the planner.
"""
from __future__ import annotations

# Per-site object / id lists that scenarios (via cpf-001) embed for the UI but that the
# response agent does NOT need to narrate a plan. Replaced by a "<key>_count" scalar in the
# LEAN copy; the full lists stay in the untrimmed result attached to the final payload.
_HEAVY_DETAIL_KEYS = {
    "committed_pj_projects", "pull_forward_pj_projects",
    "committed_pj_project_ids", "pull_forward_pj_project_ids",
    "per_site", "sites",
}
# Safety cap for any other list-of-rows so no single list can flood the prompt. Generous so
# genuine planning outputs (e.g. scn-001 per-site predictions, ~tens of rows) stay intact.
_MAX_LEAN_LIST_ROWS = 200


def lean_scenario_result(obj):
    """Return a copy of a scenario result trimmed for the RESPONSE AGENT: heavy per-site
    object/id lists are dropped (replaced by a ``<key>_count`` scalar) and any long list is
    capped. Keeps everything needed to narrate a plan (summaries, weekly counts, capacity,
    crew gap, per-group breakdowns). Generic over any scenario's output shape — recurses
    dicts/lists, makes no scenario assumptions. The FULL, untrimmed result is preserved
    separately and attached to the final payload after the response agent runs."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _HEAVY_DETAIL_KEYS:
                out[k + "_count"] = len(v) if isinstance(v, (list, dict)) else 0
            else:
                out[k] = lean_scenario_result(v)
        return out
    if isinstance(obj, list):
        lean = [lean_scenario_result(x) for x in obj[:_MAX_LEAN_LIST_ROWS]]
        if len(obj) > _MAX_LEAN_LIST_ROWS:
            lean.append({"_note": f"{len(obj) - _MAX_LEAN_LIST_ROWS} more row(s) omitted from "
                                  f"this view; full detail attached separately."})
        return lean
    return obj


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
        f"recompute, summarise away, drop rows, or claim any data is missing. (Per-site "
        f"detail lists are trimmed here for brevity and attached separately in full.)",
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
