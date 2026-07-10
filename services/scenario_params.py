"""
Scenario parameter extraction — constrained LLM scope extraction + DETERMINISTIC
resolution.

The LLM is used ONLY to read scope out of the free-text question into a fixed 4-key
schema (duration, region, group_by) — see prompts/scenario_param_prompt.py. It does
NOT plan, choose tables, or write SQL. Everything after extraction is deterministic:

  1. Validate the extracted values against fixed allowlists (unknown → default).
  2. Resolve the look-back window to concrete start_date/end_date relative to today.
  3. Resolve smp_name from the user's project-type SELECTION (never the query text).

This split keeps the model's job narrow (language understanding) while grouping,
filtering, windowing and program scoping stay repeatable.

Returns {"filter": {...}, "group_by": <str>, "resolved": {...}} where start_date /
end_date / rgn_region / smp_name live inside `filter` (the node-call convention), and
group_by is separate. On any LLM/parse failure the defaults are used — the scenario
still runs.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_ALLOWED_GROUP_BY = ["construction_gc", "m_market", "rgn_region", "m_area", "por_category", "smp_name", "pj_project_id"]
_ALLOWED_REGION = ["SOUTH", "CENTRAL", "WEST"]
_ALLOWED_UNIT = ["day", "week", "month"]

_DEFAULT_DURATION_VALUE = 2
_DEFAULT_DURATION_UNIT = "month"
_DEFAULT_GROUP_BY = "construction_gc"


def _subtract_months(d: date, n: int) -> date:
    """Subtract n calendar months from d, clamping the day to the target month end."""
    y = d.year
    m = d.month - n
    while m <= 0:
        m += 12
        y -= 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _resolve_window(value: int, unit: str, today: date) -> tuple[str, str]:
    """Resolve a look-back (value, unit) to (start_date, end_date=today) ISO strings."""
    if unit == "day":
        start = today - timedelta(days=value)
    elif unit == "week":
        start = today - timedelta(weeks=value)
    else:  # month
        start = _subtract_months(today, value)
    return start.isoformat(), today.isoformat()


def _parse_json(content: str) -> dict:
    clean = (content or "").strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    return json.loads(clean.strip())


def resolve_smp_name(project_type_raw: str | None) -> str | None:
    """Resolve the user-selected project type to the single ``smp_name`` program that
    scopes the scenario.

    ``smp_name`` is NOT extracted from the free-text query by the LLM — it is the
    project type the user explicitly selected alongside the question (carried in
    ``state['project_type']``). This mirrors the value-extraction in
    ``agents.traversal._build_project_type_filter`` so the scenario applies the same
    program scoping as the traversal path.

    The scenario's node functions (SCOP cycle-time / pending) branch their entire
    gating regime on a SINGLE ``smp_name`` (NTM vs AHLOB are mutually exclusive), so
    this returns one program, not a list:

      - empty / None       -> None (no smp_name filter)
      - "NAS" (only)       -> None (the macro table carries no 'NAS' smp_name value)
      - single program     -> that program (drives the correct gating branch)
      - multiple programs  -> the first real program (single-program gating; logged);
                              'NAS' tokens are dropped since they carry no smp_name
    """
    if not project_type_raw:
        return None
    types = [t.strip() for t in project_type_raw.split(",") if t.strip()]
    # 'NAS' has no smp_name value on the macro table — drop it so a mixed selection
    # scopes to the real program rather than to a zero-row 'NAS' equality.
    programs = [t for t in types if t.upper() != "NAS"]
    if not programs:
        return None
    if len(programs) > 1:
        logger.info(
            "Scenario: multiple project types %s selected; scoping to '%s' "
            "(node gating is single-program).", programs, programs[0]
        )
    return programs[0]


def resolve_build_plan_project_type(project_type_raw: str | None) -> str:
    """Map the user's project-type selection to the value cpf-001's ``build_plan``
    expects for ``project_type`` — 'AHLOB' or 'NTM' — which selects the SLA DAG and
    productivity fallback. Anything not AHLOB defaults to NTM."""
    prog = (resolve_smp_name(project_type_raw) or "").upper()
    return "AHLOB" if "AHLOB" in prog else "NTM"


def _coerce_field(spec: dict, raw):
    """Validate one LLM-extracted value against its field spec; fall back to the
    field's declared default on any mismatch.

    Types: int, number, enum, string, enum_list. ``enum_list`` accepts a JSON array
    (or a single value) and keeps only the entries in ``allowed`` — used when the
    question can request several values at once (e.g. break down by market AND GC)."""
    default = spec.get("default")
    ftype = (spec.get("type") or "string").lower()
    if raw is None:
        return default
    try:
        if ftype == "int":
            return int(raw)
        if ftype == "number":
            return float(raw)
        if ftype == "enum":
            for allowed in (spec.get("allowed") or []):
                if str(raw).strip().lower() == str(allowed).strip().lower():
                    return allowed  # canonical value from the allowlist
            return default
        if ftype == "enum_list":
            allow = spec.get("allowed") or []
            items = raw if isinstance(raw, list) else [raw]
            out: list = []
            for item in items:
                for allowed in allow:
                    if str(item).strip().lower() == str(allowed).strip().lower() and allowed not in out:
                        out.append(allowed)
            return out or default
        s = str(raw).strip()
        return s or default
    except (ValueError, TypeError):
        return default


def extract_params_by_schema(
    query: str,
    param_schema: dict,
    *,
    project_type: str | None = None,
    today: date | None = None,
) -> dict:
    """GENERIC, schema-driven scenario param extraction — no per-scenario few-shots.

    A single constrained LLM call fills EXACTLY the fields declared in
    ``param_schema['fields']`` (each field's type / allowed / description guides the
    model — see prompts.scenario_param_prompt.SCENARIO_PARAM_SCHEMA_SYSTEM). Values are
    then validated/defaulted deterministically. The field named by
    ``param_schema['group_by_field']`` is lifted to ``group_by``; the rest become the
    ``filter`` dict. ``smp_name`` + ``project_type`` are injected from the user's
    project-type selection (never query-derived). Never raises — falls back to the
    declared defaults so the scenario still runs.

    Scenario-specific math (e.g. rate x increase x horizon -> target count) is NOT done
    here; it lives in the node's own ``scn_python_function`` orchestrator, keeping this
    service reusable across scenarios.
    """
    fields = (param_schema or {}).get("fields") or []
    group_by_field = (param_schema or {}).get("group_by_field")

    # Deterministic baseline: every field starts at its declared default.
    values = {f["name"]: f.get("default") for f in fields if f.get("name")}

    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        from services.llm_provider import LLMProvider
        from prompts.scenario_param_prompt import SCENARIO_PARAM_SCHEMA_SYSTEM

        # Only the fields the model needs to reason about (name/type/allowed/description).
        schema_for_llm = {
            "fields": [
                {k: f[k] for k in ("name", "type", "allowed", "description") if k in f}
                for f in fields
            ]
        }
        llm = LLMProvider.get_llm("heavy")
        resp = llm.invoke([
            SystemMessage(content=SCENARIO_PARAM_SCHEMA_SYSTEM),
            HumanMessage(content=json.dumps({"schema": schema_for_llm, "question": query or ""})),
        ])
        data = _parse_json(resp.content)
        for f in fields:
            if f.get("name"):
                values[f["name"]] = _coerce_field(f, data.get(f["name"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Schema-driven param extraction failed (using defaults): %s", exc)

    group_by = values.get(group_by_field) if group_by_field else None

    # filter = all declared params except the group_by field, dropping None (optional).
    filt: dict = {
        k: v for k, v in values.items()
        if v is not None and k != group_by_field
    }

    # Program scoping comes from the selection, not the query text.
    smp_name = resolve_smp_name(project_type)
    if smp_name:
        filt["smp_name"] = smp_name
        filt["project_type"] = resolve_build_plan_project_type(project_type)

    return {
        "filter": filt,
        "group_by": group_by,
        "resolved": {**values, "group_by": group_by, "smp_name": smp_name},
    }


def extract_scenario_params(
    query: str, *, today: date | None = None, project_type: str | None = None
) -> dict:
    """Extract + resolve scenario scope params. Never raises — falls back to defaults.

    The LLM fills a fixed 4-key schema (duration/region/group_by); the values are then
    validated against allowlists and the window is resolved deterministically.
    ``project_type`` is the user's explicit selection (state['project_type']); it is
    resolved to a single ``smp_name`` and injected into ``filter`` deterministically
    (never LLM-extracted).
    """
    today = today or date.today()

    duration_value = _DEFAULT_DURATION_VALUE
    duration_unit = _DEFAULT_DURATION_UNIT
    region = None
    group_by = _DEFAULT_GROUP_BY

    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        from services.llm_provider import LLMProvider
        from prompts.scenario_param_prompt import SCENARIO_PARAM_SYSTEM

        llm = LLMProvider.get_llm("heavy")
        resp = llm.invoke([
            SystemMessage(content=SCENARIO_PARAM_SYSTEM),
            HumanMessage(content=query or ""),
        ])
        data = _parse_json(resp.content)

        dv, du = data.get("duration_value"), (data.get("duration_unit") or "")
        if isinstance(dv, int) and dv > 0 and isinstance(du, str) and du.lower() in _ALLOWED_UNIT:
            duration_value, duration_unit = dv, du.lower()

        r = data.get("rgn_region")
        if isinstance(r, str) and r.strip().upper() in _ALLOWED_REGION:
            region = r.strip().upper()

        gb = data.get("group_by")
        if isinstance(gb, str) and gb.strip() in _ALLOWED_GROUP_BY:
            group_by = gb.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scenario param extraction failed (using defaults): %s", exc)

    start_date, end_date = _resolve_window(duration_value, duration_unit, today)
    filt: dict = {"start_date": start_date, "end_date": end_date}
    if region:
        filt["rgn_region"] = region

    # smp_name comes from the user's project-type selection, not the query text.
    smp_name = resolve_smp_name(project_type)
    if smp_name:
        filt["smp_name"] = smp_name

    return {
        "filter": filt,
        "group_by": group_by,
        "resolved": {
            "duration_value": duration_value,
            "duration_unit": duration_unit,
            "rgn_region": region,
            "group_by": group_by,
            "smp_name": smp_name,
        },
    }