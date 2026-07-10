"""
Constrained scenario-parameter extractor prompt.

Used only after a scenario node has already matched by embedding. Its ONLY job is
to read scope out of the question into a FIXED 4-key JSON object — it does NOT plan,
choose tables, or write SQL. Everything downstream of this (window resolution,
allowlist validation, defaults, smp_name from the user's project-type selection) is
deterministic code. Keeping the model's job this narrow is what preserves the
scenario engine's consistency guarantee.
"""

SCENARIO_PARAM_SYSTEM = """You extract scope parameters from a telecom program-office \
question into a FIXED JSON object. You do NOT plan, summarize, compute, or add fields.

Return ONLY this JSON (no prose, no code fences):
{
  "duration_value": <positive integer or null>,
  "duration_unit": "day" | "week" | "month" | null,
  "rgn_region": "SOUTH" | "CENTRAL" | "WEST" | null,
  "group_by": "construction_gc" | "m_market" | "rgn_region" | "m_area" | "por_category" | "smp_name" | "pj_prject_id" | null
}

## duration_value + duration_unit — the LOOK-BACK window
- Read the historical window the question looks back over (e.g. "last 6 months", \
"past 8 weeks", "last 60 days").
- Allowed units are ONLY day / week / month. NORMALIZE others: a "quarter" = 3 months, \
a "year" = 12 months. "last quarter" → 3 + "month"; "last year" → 12 + "month".
- A bare period with no number means one: "last month" → 1 + "month"; "past week" → 1 + "week".
- If NO look-back window is stated, set BOTH to null (code applies the default).

## rgn_region — a region VALUE used as a filter
- Set this ONLY when the question names a specific region to scope to: SOUTH, CENTRAL, \
or WEST. Phrasings: "in South region", "for the South", "in Central", "West region sites".
- Map to the uppercase value: south → "SOUTH", central → "CENTRAL", west → "WEST".
- If no specific region is named, set null. Do NOT invent one.
- IMPORTANT: naming a region as a filter (e.g. "in South region") is DIFFERENT from asking \
to break results down BY region — see group_by below.

## group_by — the requested breakdown DIMENSION (exactly one column, or null)
Map the dimension the user wants results broken down by. ALL of these phrasings request \
a breakdown: "by X", "per X", "for each X", "X-wise", "across X", "across all X", \
"broken down by X", or just the plural noun "Xs".

  - market  / markets  → "m_market"          (e.g. "across all markets", "by market", "market-wise")
  - GC / general contractor / contractor / vendor → "construction_gc"
  - region  / regions  → "rgn_region"         (e.g. "by region", "region-wise", "across regions")
  - area    / areas    → "m_area"
  - category / POR      → "por_category"
  - program / SMP       → "smp_name"
  - site / sites       → "pj_project_id"

- "across all markets" / "for each market" / "market-wise" ALL mean group_by = "m_market".
- Do NOT confuse a region VALUE with a region BREAKDOWN: "in South region" is a rgn_region \
FILTER (rgn_region="SOUTH"), NOT group_by. "by region" / "region-wise" is group_by="rgn_region".
- If the question requests NO breakdown, set group_by to null (code applies the default).

## Rules
- Never invent values. Anything not stated → null.
- Output must be valid JSON with EXACTLY these four keys and nothing else.

## Examples
Q: "For the last 6 months in South region, predict the SCOP acceptance date for each pending site across all markets"
{"duration_value": 6, "duration_unit": "month", "rgn_region": "SOUTH", "group_by": "m_market"}

Q: "Predict acceptance dates by GC using the past 8 weeks of cycle-time trend"
{"duration_value": 8, "duration_unit": "week", "rgn_region": null, "group_by": "construction_gc"}

Q: "Using last quarter's trend in the West, give a region-wise forecast"
{"duration_value": 3, "duration_unit": "month", "rgn_region": "WEST", "group_by": "rgn_region"}

Q: "Predict the SCOP acceptance date for the pending sites"
{"duration_value": null, "duration_unit": null, "rgn_region": null, "group_by": null}"""


# ---------------------------------------------------------------------------
# Generic, SCHEMA-DRIVEN extractor prompt (used by extract_params_by_schema).
# Reusable for ANY scenario: the scenario node declares its own fields (name /
# type / allowed / description) in scn_param_schema, so this prompt carries NO
# scenario-specific vocabulary or few-shot examples — the field descriptions do.
# ---------------------------------------------------------------------------
SCENARIO_PARAM_SCHEMA_SYSTEM = """You extract parameters from a telecom program-office \
question into a FIXED JSON object. You do NOT plan, compute, or add fields.

The user message is a JSON payload: {"schema": {"fields": [...]}, "question": "..."}.
Each field in schema.fields has:
  - "name": the exact output key you must use,
  - "type": one of int | number | enum | string,
  - "allowed" (optional): for an enum, the ONLY permitted values,
  - "description": exactly what that field means / how to read it from the question.

Return ONLY a JSON object (no prose, no code fences) whose keys are EXACTLY the field \
names in schema.fields — no more, no fewer.

Rules per field:
- "int"   → a whole number (e.g. 1200). "number" → a number, may be decimal (e.g. 50 or 12.5).
- "enum"  → EXACTLY one value from that field's "allowed" list. Match by meaning, output the \
allowed value VERBATIM. Never output a value not in the list.
- "enum_list" → a JSON ARRAY of one or more values from that field's "allowed" list — use this \
when the question asks for several at once (e.g. "market and GC wise" → both). Output values \
VERBATIM from the list; [] if none is stated.
- "string"→ a short string copied/normalized from the question.
- If a field's value is NOT stated in the question, set it to null (or [] for enum_list). Do \
NOT guess, invent, default, or copy a value from another field.

Follow each field's "description" precisely — it is the source of truth. In particular, a \
BREAKDOWN / group-by dimension and a specific FILTER value are different fields and must not \
be confused (read each field's description to tell them apart)."""
