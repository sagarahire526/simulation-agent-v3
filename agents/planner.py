"""
Planner Agent — Multi-step parallel execution node.

Workflow:
  1. Fetch semantic context (KPIs, question bank, simulation scenarios).
  2. Use an LLM to decompose the user query into N focused sub-queries (plan steps).
  3. Execute each sub-query against the Traversal Agent concurrently via asyncio.gather()
     running in a dedicated thread with its own event loop.
  4. Accumulate all traversal results and pass them to the Response Agent.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import warnings
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage

from models.state import SimulationState
from services.llm_provider import LLMProvider
from agents.traversal import atraversal_node
from services.semantic_service import SemanticService
from services import internal_scenarios as scenario_lib
from services.sse_context import emit_sse
from prompts.planner_prompt import PLANNER_SYSTEM
from services.date_context import today_date_context

logger = logging.getLogger(__name__)

_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_MAX_PARALLEL_STEPS = 6    # Hard cap — prompt targets 4-6 focused steps
_PLANNER_STEP_MAX_STEPS = 10  # Budget: get_kpi + run_sql_python + 3 retries + get_node fallback + run_sql_python + spare
_STEP_TIMEOUT_SEC = 300   # Kill a runaway sub-traversal after 5 minutes
_GCL_SIM_THRESHOLD = 0.8  # GCL `simulation` rows below this don't count as a real match

# Shared executor for running asyncio event loops from sync planner nodes.
# Bounded to 4 threads so 100 concurrent requests don't spawn 100 OS threads.
_planner_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="planner-async")



def _fetch_gcl_context(query: str) -> dict:
    """
    Run the GCL semantic search and produce the planner-facing strings.
    Always returns a dict with stable keys; on error every field is empty.
    """
    try:
        semantic = SemanticService()
        data = semantic.get_all_context(query)
        sim_rows = data.get("simulation", []) or []
        sim_strong = sum(
            1 for r in sim_rows
            if (r.get("similarity_score") or 0) >= _GCL_SIM_THRESHOLD
        )
        return {
            "formatted": semantic.format_traversal_context(data) if any(data.values()) else "",
            "guidance":  semantic.format_simulation_guidance(data) if any(data.values()) else "",
            "analysis":  SemanticService.extract_headings(data) if any(data.values()) else {},
            "hits":      (
                len(data.get("kpi", [])),
                len(data.get("question_bank", [])),
                len(sim_rows),
            ),
            "sim_strong": sim_strong,
        }
    except Exception as e:
        logger.warning("GCL semantic search failed (non-fatal): %s", e)
        return {"formatted": "", "guidance": "", "analysis": {}, "hits": (0, 0, 0), "sim_strong": 0}


def _fetch_internal_scenarios(query: str) -> list[dict]:
    """
    Run the Internal Scenario Library lookup. Returns [] on any error so the
    planner can proceed with GCL-only context.
    """
    try:
        return scenario_lib.search(query)
    except Exception as e:
        logger.warning("Internal scenario search failed (non-fatal): %s", e)
        return []


def _fetch_scenario_node_match(query: str, project_type: str) -> dict | None:
    """
    Look up a matching entity_type='scenario' GRAPH node (embedding search sliced
    to scenario nodes, gated on the node's own scn_similarity_threshold). Returns
    {node_id, label, score, threshold} or None. Non-fatal on any error.
    """
    try:
        from services.schema_embedding_service import search_scenarios
        return search_scenarios(query, project_type=project_type)
    except Exception as e:
        logger.warning("Scenario-node match failed (non-fatal): %s", e)
        return None


def _fetch_scenario_param_schema(node_id: str) -> dict | None:
    """Fetch + parse a scenario node's `scn_param_schema` from Neo4j. Returns the parsed
    dict (expected to carry a `fields` list for schema-driven extraction), or None if
    absent/legacy/unparseable. Non-fatal on any error."""
    try:
        from tools.neo4j_tool import Neo4jTool
        out = Neo4jTool().run_cypher_safe(
            "MATCH (n:BKGNode {node_id: $nid}) RETURN coalesce(n.scn_param_schema, '') AS s",
            {"nid": node_id},
        )
        records = (out.get("records") or out.get("results") or []) if isinstance(out, dict) else []
        raw = (records[0].get("s") if records else "") or ""
        schema = json.loads(raw) if raw.strip() else None
        return schema if isinstance(schema, dict) else None
    except Exception as e:
        logger.warning("Scenario param-schema fetch failed (non-fatal): %s", e)
        return None


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


def _render_scenario_findings(label: str, params: dict, result) -> str:
    """Render ANY scenario's result as verbatim markdown so the response agent sees the
    real numbers. Fully generic (recurses arbitrarily nested dicts/lists) — makes no
    assumptions about a scenario's output shape.

    The scenario is already fully computed by the graph nodes; this is render-only.
    """
    lines = [
        f"DETERMINISTIC SCENARIO RESULT — '{label}'. Already computed by the graph "
        f"nodes (no LLM grouping/filtering). RENDER THE DATA BELOW VERBATIM — do NOT "
        f"recompute, summarise away, drop rows, or claim any data is missing.",
        "",
        f"Resolved scope: {params.get('resolved', {})}",
        "",
    ]
    if not isinstance(result, dict):
        lines.append(str(result))
        return "\n".join(lines)

    for key, val in result.items():
        _render_block(key, val, lines, level=4)
    return "\n".join(lines)


def _run_scenario_bypass(scenario_match: dict, query: str, project_type: str = "") -> dict | None:
    """
    Deterministically execute a matched scenario node — NO planner LLM, NO
    traversal LLM. Extracts scope params (constrained), runs the scenario's
    orchestrator via the sandbox `run_scenario` helper, and returns a
    planner_step_results-shaped payload. Returns None to fall through to the
    normal LLM planning path on any failure.

    `project_type` is the user's explicit selection; it is resolved to the
    scenario's `smp_name` scope deterministically (not LLM-extracted).
    """
    try:
        import time as _time
        from services.scenario_params import extract_scenario_params, extract_params_by_schema
        from tools.python_sandbox import PythonSandbox

        scn_id = scenario_match["node_id"]

        # Param extraction is scenario-aware: if the node declares a `fields` schema in
        # scn_param_schema, use the GENERIC schema-driven extractor (no per-scenario
        # few-shots); otherwise fall back to the legacy SCOP extractor (scn-001).
        schema = _fetch_scenario_param_schema(scn_id)
        if isinstance(schema, dict) and schema.get("fields"):
            params = extract_params_by_schema(query, schema, project_type=project_type)
        else:
            params = extract_scenario_params(query, project_type=project_type)

        # Surface the deterministically-resolved scope in the terminal so grouping /
        # filtering / window / program are auditable per run.
        print(
            f"  {_CYAN}🎯 scenario params{_RESET} "
            f"{_DIM}(node={scn_id}){_RESET}\n"
            f"{json.dumps(params, indent=2)}",
            flush=True,
        )
        logger.info("Scenario params (%s): %s", scn_id, json.dumps(params))

        # Pass params as JSON data (no code injection) into the sandbox call.
        payload = json.dumps({
            "scenario_id": scn_id,
            "filter": params["filter"],
            "group_by": params["group_by"],
        })
        code = (
            "import json as _json\n"
            f"_p = _json.loads({payload!r})\n"
            "result = run_scenario(_p['scenario_id'], _p['filter'], _p['group_by'])"
        )

        t0 = _time.perf_counter()
        out = PythonSandbox().execute(code, timeout_seconds=120)
        dt = (_time.perf_counter() - t0) * 1000.0

        if out.get("status") != "success":
            logger.warning("Scenario bypass execution error: %s", out.get("error"))
            return None

        result = out.get("result") or {}
        preds = result.get("predictions", []) if isinstance(result, dict) else []

        # Embed the full result as verbatim markdown so the response agent sees the
        # ACTUAL rows (generic over any scenario's output shape) — not just a summary.
        findings = _render_scenario_findings(
            scenario_match.get("label", "scenario"), params, result
        )
        step = {
            "traversal_findings": findings,
            "traversal_tool_calls": [{
                "tool_name": "run_scenario",
                "tool_input": {
                    "scenario_id": scn_id,
                    "filter": params["filter"],
                    "group_by": params["group_by"],
                },
                "tool_output": result,
                "status": "success",
                "execution_time_ms": round(dt, 1),
            }],
            "traversal_steps_taken": 1,
        }
        return {"step_result": step, "resolved_params": params, "row_count": len(preds)}
    except Exception as e:
        logger.warning("Scenario bypass failed (falling back to planner): %s", e)
        return None


def _parse_planner_response(content: str) -> tuple[str, list[str]]:
    """
    Parse the LLM's JSON plan output.
    Returns (planning_rationale, steps_list).
    Falls back to a single-step plan on parse failure.
    """
    try:
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        data = json.loads(clean.strip())
        rationale = data.get("planning_rationale", "")
        steps = data.get("steps", [])
        if not steps or not isinstance(steps, list):
            raise ValueError("No steps found in planner response")
        return rationale, [str(s) for s in steps if str(s).strip()]
    except (json.JSONDecodeError, ValueError, IndexError):
        logger.warning("Planner LLM returned non-JSON or empty steps; using single-step fallback.")
        return "Single-step fallback due to parse error.", []


async def _run_traversal_step_async(
    step_query: str,
    base_state: SimulationState,
    step_idx: int,
    max_steps: int = _PLANNER_STEP_MAX_STEPS,
) -> dict:
    """Run one planning step via the async traversal node."""
    warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")
    step_state: SimulationState = {
        **base_state,
        "user_query": step_query,
        "refined_query": step_query,
        "max_traversal_steps": max_steps,
    }
    try:
        return await atraversal_node(step_state)
    except Exception as e:
        logger.error("Traversal step %d failed for query '%s': %s", step_idx + 1, step_query[:80], e)
        return {
            "traversal_findings": f"Step failed: {e}",
            "traversal_tool_calls": [],
            "traversal_steps_taken": 0,
            "errors": [f"Traversal step error: {e}"],
        }


async def _gather_traversals(steps: list[str], state: SimulationState) -> list:
    """
    Run all traversal steps concurrently inside a single asyncio event loop.
    Each step uses agent.ainvoke() so they truly overlap during LLM I/O waits.
    Emits SSE planner_step_complete events as each step finishes.
    """
    # Wrap each step in a task that tags the result with its index
    async def _tagged_step(idx: int, step: str) -> tuple[int, dict | Exception]:
        try:
            result = await asyncio.wait_for(
                _run_traversal_step_async(step, state, idx),
                timeout=float(_STEP_TIMEOUT_SEC),
            )
            return idx, result
        except Exception as exc:
            return idx, exc

    pending = [_tagged_step(i, s) for i, s in enumerate(steps)]
    results: list[dict | Exception] = [None] * len(steps)  # type: ignore[list-item]

    for coro in asyncio.as_completed(pending):
        step_idx, result = await coro
        results[step_idx] = result

        # Emit SSE progress event
        step_query = steps[step_idx]
        display_query = step_query.split(": ", 1)[1] if ": " in step_query else step_query
        is_error = isinstance(result, Exception)
        emit_sse("planner_step_complete", {
            "step_index": step_idx,
            "step_total": len(steps),
            "step_query": display_query,
            "status": "error" if is_error else "complete",
            "error": str(result) if is_error else None,
        })

    return results


def planner_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Planner Agent (sync — required by LangGraph's sync stream API).

    Reads:  refined_query, max_traversal_steps
    Writes: planner_steps, planner_step_results,
            scenario_simulation_guidance, current_phase, messages
    """
    refined_query = state.get("refined_query") or state["user_query"]

    print(f"\n{_BOLD}{'═' * 70}", flush=True)
    print(f"  📋 PLANNER AGENT — Decomposing query into parallel steps", flush=True)
    print(f"{'═' * 70}{_RESET}\n", flush=True)
    print(f"  {_DIM}Query: {refined_query}{_RESET}\n", flush=True)

    # ── Step 1: Fetch semantic context for planning guidance ──────────────────
    semantic_context = ""
    simulation_guidance = ""
    semantic_analysis: dict[str, list[str]] = {}
    internal_matches: list[dict] = []
    sim_strong = 0

    # Run GCL semantic search and the local Internal Scenario Library lookup
    # in parallel — both feed the planner via the same semantic_context block,
    # and the planner picks the source with the higher similarity score.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="planner-ctx") as ctx_pool:
        gcl_future = ctx_pool.submit(_fetch_gcl_context, refined_query)
        internal_future = ctx_pool.submit(_fetch_internal_scenarios, refined_query)

        try:
            gcl = gcl_future.result()
            semantic_context     = gcl["formatted"]
            simulation_guidance  = gcl["guidance"]
            semantic_analysis    = gcl["analysis"]
            kpi_hits, qb_hits, sim_hits = gcl["hits"]
            sim_strong = gcl.get("sim_strong", 0)
            if any([kpi_hits, qb_hits, sim_hits]):
                print(
                    f"  {_GREEN}🎯 Semantic context: "
                    f"{kpi_hits} KPI · {qb_hits} Q&A · {sim_hits} scenario(s){_RESET}",
                    flush=True,
                )
            else:
                print(f"  {_DIM}ℹ  No semantic context (API may be unreachable).{_RESET}", flush=True)
        except Exception as e:
            logger.warning("Semantic search in planner failed (non-fatal): %s", e)

        try:
            internal_matches = internal_future.result()
            if internal_matches:
                top = internal_matches[0]
                print(
                    f"  {_GREEN}📚 Internal Library: "
                    f"matched '{top.get('tag')}' "
                    f"(similarity {top['similarity_score'] * 100:.1f}%){_RESET}",
                    flush=True,
                )
            else:
                print(
                    f"  {_DIM}ℹ  Internal Library: no match "
                    f"(threshold {scenario_lib.MIN_SIMILARITY:.2f}).{_RESET}",
                    flush=True,
                )
        except Exception as e:
            logger.warning("Internal scenario lookup failed (non-fatal): %s", e)

    # ── Scenario-match gate ──────────────────────────────────────────────────
    # A query is considered "covered by an approved scenario" only when at least
    # one source clears its threshold: GCL sim row ≥ 0.8, or internal library
    # match above MIN_SIMILARITY (also 0.8). Neither → False; the UI uses this
    # to surface a "no matching scenario" notice when the user re-opens the q.
    scenario_match_found = bool(sim_strong) or bool(internal_matches)
    emit_sse("scenario_match_status", {"matched": scenario_match_found})

    # ── Deterministic scenario-node bypass ──────────────────────────────────
    # If an entity_type='scenario' GRAPH node matches >= its own threshold, run it
    # deterministically (no planner LLM, no traversal LLM) and return the assembled
    # rows straight into planner_step_results. This is the consistency path — the
    # scenario's orchestrator calls its contributing nodes with fixed group_by/
    # filters, so grouping/filtering/computation are fully repeatable.
    # Match on the RAW user query, not the refined one: a scenario's canonical question
    # is written in the user's own phrasing, and the query refiner reword/normalization
    # can drift a verbatim query well below the similarity threshold. Param extraction
    # below still uses refined_query (entity names normalized, e.g. Chicago -> CHICAGO,
    # which the filters need to be case-correct).
    scenario_match_query = state.get("user_query") or refined_query
    scenario_node = _fetch_scenario_node_match(scenario_match_query, state.get("project_type", ""))
    if scenario_node:
        bypass = _run_scenario_bypass(scenario_node, refined_query, state.get("project_type", ""))
        if bypass is not None:
            print(
                f"  {_GREEN}⚡ Deterministic scenario bypass: '{scenario_node.get('label')}' "
                f"(sim {scenario_node.get('score')}) — skipping LLM planner + traversal{_RESET}",
                flush=True,
            )
            emit_sse("planner_plan_ready", {
                "step_total": 1,
                "steps": [f"Deterministic scenario: {scenario_node.get('label')}"],
                "rationale": "Matched an approved scenario node; executed deterministically.",
            })
            return {
                "planning_rationale": f"Deterministic scenario bypass: {scenario_node.get('label')}",
                "planner_steps": [f"Scenario: {scenario_node.get('label')}"],
                "planner_step_results": [bypass["step_result"]],
                "scenario_simulation_guidance": "",
                "scenario_match_found": True,
                "planner_semantic_context": "",
                "semantic_analysis": {},
                "current_phase": "response",
                "messages": [{
                    "agent": "planner",
                    "content": (
                        f"Deterministic scenario '{scenario_node.get('label')}' executed "
                        f"({bypass['row_count']} rows); LLM planning + traversal bypassed. "
                        f"Resolved scope: {bypass['resolved_params']['resolved']}."
                    ),
                }],
            }

    # Splice the internal-library block onto the GCL block so the planner sees
    # both signals with their similarity scores under the same Mode A logic.
    if internal_matches:
        internal_block = scenario_lib.format_for_planner(internal_matches)
        if internal_block:
            semantic_context = (
                (semantic_context + "\n\n" if semantic_context else "")
                + internal_block
            )

    # ── Step 2: LLM creates the plan (planner tier — strong reasoning for
    #            fact-vs-gap judgement and step decomposition) ──
    llm = LLMProvider.get_llm("planner")

    # Escape any literal { } in dynamic content before calling str.format()
    safe_semantic = semantic_context.replace("{", "{{").replace("}", "}}")

    planning_prompt = PLANNER_SYSTEM.format(
        today_date=today_date_context(),
        semantic_context=safe_semantic,
    )

    llm_response = llm.invoke([
        SystemMessage(content=planning_prompt),
        HumanMessage(content=refined_query),
    ])

    rationale, steps = _parse_planner_response(llm_response.content)

    # Safety: if parsing failed, fall back to single traversal step on the full query
    if not steps:
        steps = [f"Sub-query 1: {refined_query}"]

    # Cap the number of parallel steps
    steps = steps[:_MAX_PARALLEL_STEPS]

    print(f"\n  {_BOLD}Business Analysis Plan ({len(steps)} steps):{_RESET}", flush=True)
    if rationale:
        print(f"  {_YELLOW}📌 Intent:{_RESET} {rationale}\n", flush=True)
    display_steps = []
    for i, step in enumerate(steps, 1):
        display = step
        if ": " in step:
            display = step.split(": ", 1)[1]
        print(f"  {_CYAN}  Step {i}:{_RESET} {display}", flush=True)
        display_steps.append(display)
    print(flush=True)

    # ── SSE: plan is ready, sub-queries about to start ────────────────────────
    emit_sse("planner_plan_ready", {
        "step_total": len(steps),
        "steps": display_steps,
        "rationale": rationale,
    })

    # ── Step 3: Execute each step concurrently ────────────────────────────────
    # Strategy: run asyncio.gather() inside a dedicated thread that owns its own
    # event loop. This avoids "cannot call asyncio.run() from a running loop"
    # errors that occur when LangGraph's sync runner has its own internal loop,
    # while still getting true async concurrency across all traversal sub-steps.
    print(f"  {_BOLD}Executing {len(steps)} traversal(s) in parallel…{_RESET}\n", flush=True)

    # Inject semantic context into state so sub-traversals can reuse it
    # (the planner's return value sets this field too late for the sub-traversals)
    traversal_state: SimulationState = {
        **state,
        "planner_semantic_context": semantic_context,
        "scenario_simulation_guidance": simulation_guidance,
    }

    ctx = contextvars.copy_context()
    future = _planner_executor.submit(ctx.run, asyncio.run, _gather_traversals(steps, traversal_state))
    gathered = future.result(timeout=_STEP_TIMEOUT_SEC + 60)

    step_results: list[dict] = []
    for idx, result in enumerate(gathered):
        if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
            logger.warning("Step %d timed out after %ds", idx + 1, _STEP_TIMEOUT_SEC)
            step_results.append({
                "traversal_findings": f"Step timed out after {_STEP_TIMEOUT_SEC}s",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
                "errors": [f"Step {idx + 1} timed out"],
            })
        elif isinstance(result, Exception):
            logger.error("Unexpected error in step %d: %s", idx + 1, result)
            step_results.append({
                "traversal_findings": f"Unexpected error: {result}",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
            })
        else:
            step_results.append(result)

    total_tool_calls = sum(
        r.get("traversal_steps_taken", 0) for r in step_results
    )
    print(f"\n  {_GREEN}✅ All steps complete — {total_tool_calls} total tool calls{_RESET}\n", flush=True)

    logger.info(
        "Planner completed: %d steps, %d total tool calls",
        len(steps), total_tool_calls,
    )

    return {
        "planning_rationale": rationale,
        "planner_steps": steps,
        "planner_step_results": step_results,
        "scenario_simulation_guidance": simulation_guidance,
        "scenario_match_found": scenario_match_found,
        "planner_semantic_context": semantic_context,
        "semantic_analysis": semantic_analysis,
        "current_phase": "response",
        "messages": [{
            "agent": "planner",
            "content": (
                f"Planning complete: {len(steps)} steps executed in parallel, "
                f"{total_tool_calls} total traversal tool calls."
            ),
        }],
    }
