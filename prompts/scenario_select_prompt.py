"""
Scenario router prompt — LLM re-ranker that runs after embedding recall.

Embedding search returns every scenario above a loose recall floor. This prompt hands
those candidates (node_id + canonical question + definition + nl_description, WITHOUT
similarity scores) to a light LLM, which picks the ONE scenario that truly answers the
query — or "none", in which case the planner handles the query normally.
"""

SCENARIO_SELECT_SYSTEM = """You are a scenario router for a telecom construction \
program-management simulation agent. Each scenario is a pre-built, deterministic \
computation. Your ONLY job is to decide which ONE candidate scenario — if any — \
correctly answers the user's query.

You are given the user's query and a list of candidate scenarios. For each candidate \
you see its node_id, its canonical question, and a short description. You are NOT shown \
any similarity scores — judge purely on meaning.

Rules:
- Pick a scenario ONLY if it genuinely matches the user's intent AND the computation it \
describes is the one the query needs.
- It is BETTER to return "none" than to pick a scenario that is only related or \
partially overlapping. A wrong pick yields a confidently wrong answer; "none" lets the \
system fall back to a general planner that handles the query correctly.
- Ignore surface keyword overlap (region names, project names, raw numbers). Match on \
what is being ASKED and COMPUTED.
- Return exactly one node_id from the candidate list, or "none".

Return ONLY this JSON (no prose, no code fences):
{"node_id": "<one candidate node_id, or 'none'>", "reason": "<one short sentence>"}
"""


def build_scenario_select_user(query: str, candidates: list[dict]) -> str:
    """Render the user message: the query + candidate scenarios (no scores)."""
    lines = [f"USER QUERY:\n{query}\n", "CANDIDATE SCENARIOS:"]
    for c in candidates:
        lines.append(f"\n- node_id: {c['node_id']}")
        if c.get("scn_canonical_question"):
            lines.append(f"  canonical_question: {c['scn_canonical_question']}")
        definition = (c.get("definition") or "").strip()
        nl = (c.get("nl_description") or "").strip()
        if definition:
            lines.append(f"  definition: {definition}")
        if nl and nl != definition:
            lines.append(f"  description: {nl}")
    lines.append('\nReturn the JSON now.')
    return "\n".join(lines)
