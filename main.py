"""
CLI entry point for the Simulation Agent system.

Usage:
    python -m simulation_agent.main "Complete 300 sites in Chicago in 2 weeks"
    python -m simulation_agent.main --interactive
"""
from __future__ import annotations

import sys
import json
import logging
import argparse
from datetime import datetime

from graph import run_simulation
from tools.neo4j_tool import neo4j_tool
from fastapi import FastAPI
import uvicorn
from fastapi.middleware.cors import CORSMiddleware


# ── Logging setup ──
# force=True ensures we replace any handlers uvicorn already attached,
# preventing duplicate log lines when running under uvicorn --reload.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-30s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)

# Suppress verbose HTTP request logs from httpx (e.g. OpenAI API calls)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


"""
FastAPI application factory.

Creates the app, registers middleware, and mounts versioned routers.
No business logic lives here.

Run:
    uvicorn api.app:app --reload --port 8000

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc
"""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi.responses import FileResponse
from api.v1.router import router as v1_router
import services.db_service as db_svc


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_svc.ensure_tables()
    yield
    db_svc.close_pool()


app = FastAPI(
    lifespan=lifespan,
    title="Simulation Agent API",
    description=(
        "LangGraph multi-agent system backed by Neo4j (BKG) and PostgreSQL.\n\n"
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", tags=["Root"])
async def root():
    return{
        "service": "Simulator service",
        "version": "1.0.0",
        "docs": "/docs"
    }

app.include_router(v1_router, prefix="/api")


@app.get("/bkg-admin", tags=["BKG Admin UI"], include_in_schema=False)
async def bkg_admin_ui():
    """Serve the single-page BKG admin interface."""
    html_path = Path(__file__).parent / "static" / "bkg_admin.html"
    return FileResponse(html_path)

if __name__ == "__main__":
    import os

    reload = os.getenv("RELOAD", "false").lower() == "true"
    workers = int(os.getenv("WORKERS", "1"))

    # NOTE: Using workers > 1 requires replacing MemorySaver with a
    # persistent checkpointer (e.g. PostgresSaver) so HITL state is
    # shared across worker processes.  Keep workers=1 until that migration.
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload,
        workers=1 if reload else workers,
    )


// ── Construction Plan Forecast (added 2026-05-31) ──
# CREATE (:BKGNode {map_table_name: 'stg_ndpd_mbt_tmobile_macro_combined', session_id: '69a3d22f26e208edc083a06e', node_id: 'cpf-001-construction-plan-forecast', label: 'Construction Plan Forecast', name: 'construction_plan_forecast', kpi_name: 'Construction Plan Forecast', node_type: 'CORE', entity_type: 'kpi', status: 'confirmed', is_skipped: false, skip_reason: '', map_database_name: 'public', map_key_column: 'pj_project_id', map_label_column: 'pj_project_id', nl_business_rule: '', definition: 'Forecasts a week-by-week construction-start plan for a target number of sites over a configurable horizon (default 2 months). Combines (a) committed sites whose planned construction start (pj_p_4225) falls inside the window, with (b) pull-forward candidates whose planned start is after the window but whose pre-requisite completion meets a configurable threshold (default 80%). Applies a GC run-rate capacity ceiling (count of construction completions in last 60 days / 8 weeks) and flags weeks that exceed it. Use for planning, scheduling, forecasting, or backlog-prioritization questions of the form \'plan/schedule/forecast N sites in next M months / weeks\'.', nl_description: 'Forecasts a week-by-week construction-start plan for a target number of sites over a configurable horizon (default 2 months). Combines (a) committed sites whose planned construction start (pj_p_4225) falls inside the window, with (b) pull-forward candidates whose planned start is after the window but whose pre-requisite completion meets a configurable threshold (default 80%). Applies a GC run-rate capacity ceiling (count of construction completions in last 60 days / 8 weeks) and flags weeks that exceed it. Use for planning, scheduling, forecasting, or backlog-prioritization questions of the form \'plan/schedule/forecast N sites in next M months / weeks\'.', kpi_description: 'Forecasts a week-by-week construction-start plan for a target number of sites over a configurable horizon (default 2 months). Combines (a) committed sites whose planned construction start (pj_p_4225) falls inside the window, with (b) pull-forward candidates whose planned start is after the window but whose pre-requisite completion meets a configurable threshold (default 80%). Applies a GC run-rate capacity ceiling (count of construction completions in last 60 days / 8 weeks) and flags weeks that exceed it. Use for planning, scheduling, forecasting, or backlog-prioritization questions of the form \'plan/schedule/forecast N sites in next M months / weeks\'.', kpi_business_logic: '1) Committed pool: sites where pj_p_4225_construction_start_finish in [today, today+window_days] and ms_1550_construction_start_actual IS NULL. Bucket by ISO-week of pj_p_4225. 2) Pull-forward pool: sites where pj_p_4225 > today+window_days, not already started, and prereq_pct >= threshold. Bucket by ISO-week of forecasted Cx-ready date (NOT gated on forecast <= window). 3) prereq_pct per site = done_gates / applicable_gates, using the gate list inherited from the Prereq Readiness Rate KPI (dcf98a0e-...), with: Crane applicable only when scoping_package_crane_required = \'Yes\'; Crane done when scoping_package_crane_required IN (\'Yes\',\'No\'). 4) Forecasted Cx-ready date = (latest populated pj_a_*/ms_* among applicable+done gates) + shortest-path remaining SLA days to Cx Start, computed over kpi_sla_dag. 5) GC run-rate capacity: count rows with ms_1555_construction_complete_actual in [today-60d, today], divide by 8 -> weekly cap. Flag weekly buckets where committed+pull_forward > cap.', kpi_source_tables: ['pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined', 'pwc_macro_staging_schema.stg_nas_planned_outage_activity'], kpi_contract: '{"fn": "build_plan (defined in kpi_python_function)", "inputs": {"target_sites": {"type": "int", "required": true}, "window_days": {"type": "int", "default": 60}, "prereq_threshold": {"type": "float", "default": 0.8}, "project_type": {"type": "str", "default": "NTM", "enum": ["NTM", "AHLOB", "NAS"]}}, "output": {"summary": {"target": "int", "window_days": "int", "committed_count": "int", "pull_forward_count": "int", "total_in_window": "int", "gap_vs_target": "int"}, "weekly_buckets": [{"week_start": "date", "committed": "int", "pull_forward": "int", "total": "int", "capacity_cap": "int", "over_capacity": "bool"}], "capacity": {"method": "str", "completed_last_60d": "int", "weekly_cap": "int"}, "pull_forward_sites": [{"site_id": "str", "planned_cx": "date", "forecast_cx_ready": "date", "prereq_pct": "float", "last_milestone": "str", "remaining_sla_days": "int", "blockers": ["str"]}], "config": {"prereq_threshold": "float", "project_type": "str"}}, "invocation": "exec(kpi_python_function); result = build_plan(target_sites=N, window_days=W, prereq_threshold=T, project_type=PT, sla_dag=json.loads(kpi_sla_dag), execute_query=execute_query); # then set result and print, e.g. import json; print(json.dumps(result, default=str))"}', kpi_sla_dag: '{"NTM": {"site_assigned": {"column": null, "edges": {"entitlement_complete": 2, "pre_ntp": 2, "gc_assignment": 0}}, "entitlement_complete": {"column": "pj_a_3710_ran_entitlement_complete_finish", "edges": {"bom_in_bat": 2}}, "bom_in_bat": {"column": "pj_a_3850_bom_submitted_bom_in_bat_finish", "edges": {"bom_in_aiims": 21}}, "bom_in_aiims": {"column": "pj_a_3875_bom_received_bom_in_aims_finish", "edges": {"material_picked": 5}}, "material_picked": {"column": "pj_a_3925_msl_pickup_date_finish", "edges": {"JOIN": 4}}, "pre_ntp": {"column": "pj_a_4000_ll_ntp_received", "edges": {"site_walk": 3}}, "gc_assignment": {"column": null, "edges": {"site_walk": 7}}, "site_walk": {"column": "ms_1316_pre_con_site_walk_completed_actual", "alt_column": "ms_1321_talon_view_drone_svcs_actual", "edges": {"ready_for_scoping": 3}}, "ready_for_scoping": {"column": "ms_1323_ready_for_scoping_actual", "edges": {"scoping_validated": 7}}, "scoping_validated": {"column": "ms_1327_scoping_and_quoting_package_validated_actual", "edges": {"quote_submitted": 7, "access_confirmation": 7, "ntp": 14}}, "quote_submitted": {"column": "ms_1331_scoping_package_submitted_actual", "edges": {"cpo": 14}}, "cpo": {"column": "ms_1555_construction_complete_cpo_custom_field", "edges": {"spo": 2}}, "spo": {"column": "ms1555_construction_complete_spo_issued_date", "edges": {"JOIN": 5}}, "access_confirmation": {"column": "s_24x7_site_access", "edges": {"crane_readiness": 7, "JOIN": 7}}, "crane_readiness": {"column": "scoping_package_crane_required", "applicability": "scoping_package_crane_required == \'Yes\'", "done_when": "scoping_package_crane_required IN (\'Yes\',\'No\')", "edges": {"JOIN": 7}}, "ntp": {"column": "ms_1407_tower_ntp_validated_actual", "edges": {"JOIN": 7}}, "JOIN": {"column": null, "edges": {"cx_start": 4}}, "cx_start": {"column": "pj_p_4225_construction_start_finish", "actual": "ms_1550_construction_start_actual", "edges": {}}}, "AHLOB": {"cpo": {"column": "ms_1555_construction_complete_cpo_custom_field", "edges": {"spo": 2}}, "spo": {"column": "ms1555_construction_complete_spo_issued_date", "edges": {"talon_scoping": 1}}, "talon_scoping": {"column": "scoping_package_create_date", "edges": {"talon_scop": 1}}, "talon_scop": {"column": "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl", "edges": {"crane_readiness": 3}}, "crane_readiness": {"column": "scoping_package_crane_required", "applicability": "scoping_package_crane_required == \'Yes\'", "done_when": "scoping_package_crane_required IN (\'Yes\',\'No\')", "edges": {"nas_outage_upload": 2}}, "nas_outage_upload": {"column": "nas_activity_end_date", "edges": {"ll_ntp_ready": 1}}, "ll_ntp_ready": {"column": "pj_a_4000_ll_ntp_received", "edges": {"overall_ntp_ready": 2}}, "overall_ntp_ready": {"column": "pj_a_4075_construction_ntp_submitted_to_gc_finish", "edges": {"final_ntp_ready": 2}}, "final_ntp_ready": {"column": "pj_a_4100_construction_ntp_accepted_by_gc_finish", "edges": {"bom_ready": 2}}, "bom_ready": {"column": "pj_a_3850_bom_submitted_bom_in_bat_finish", "edges": {"bom_in_msl": 3}}, "bom_in_msl": {"column": "pj_a_3875_bom_received_bom_in_aims_finish", "edges": {"material_pickup": 5}}, "material_pickup": {"column": "pj_a_3925_msl_pickup_date_finish", "edges": {"cx_start": 3}}, "cx_start": {"column": "pj_p_4225_construction_start_finish", "actual": "ms_1550_construction_start_actual", "edges": {}}}}', kpi_capacity_method: 'gc_run_rate', kpi_run_rate_column: 'ms_1555_construction_complete_actual', kpi_run_rate_lookback_days: 60, kpi_prereq_threshold_default: 0.80, kpi_window_days_default: 60, kpi_python_function: '# Construction Plan Forecast — build_plan(target_sites, window_days, prereq_threshold,\n#                                         project_type, sla_dag, execute_query)\n# Convention: uses the sandbox-provided execute_query(sql) -> list[dict] (NOT raw cur).\n# Site identifier: s_site_id (per traversal rule; NEVER pj_project_id for site counts).\n\nfrom datetime import date, timedelta\nfrom collections import deque, defaultdict\n\ndef _applicable_nodes(sla_dag_pt, site_row):\n    applicable = set()\n    for name, spec in sla_dag_pt.items():\n        if name in ("JOIN", "cx_start"):\n            continue\n        if name == "crane_readiness":\n            if site_row.get("scoping_package_crane_required") != "Yes":\n                continue\n        applicable.add(name)\n    return applicable\n\ndef _is_done(node_name, spec, site_row):\n    if node_name == "crane_readiness":\n        return site_row.get("scoping_package_crane_required") in ("Yes", "No")\n    col = spec.get("column")\n    alt = spec.get("alt_column")\n    if col and site_row.get(col) is not None:\n        return True\n    if alt and site_row.get(alt) is not None:\n        return True\n    return False\n\ndef _last_completed_milestone(applicable, sla_dag_pt, site_row):\n    best_name, best_date = None, None\n    for name in applicable:\n        spec = sla_dag_pt[name]\n        if not _is_done(name, spec, site_row):\n            continue\n        d = site_row.get(spec.get("column")) or site_row.get(spec.get("alt_column"))\n        if d and (best_date is None or d > best_date):\n            best_name, best_date = name, d\n    return best_name, best_date\n\ndef _shortest_path_days(sla_dag_pt, start, applicable):\n    if start is None or start == "cx_start":\n        return 0\n    visited = {start: 0}\n    q = deque([start])\n    while q:\n        u = q.popleft()\n        for v, w in (sla_dag_pt.get(u, {}).get("edges") or {}).items():\n            if v not in ("JOIN", "cx_start") and v not in applicable:\n                continue\n            nd = visited[u] + (w or 0)\n            if v not in visited or nd < visited[v]:\n                visited[v] = nd\n                q.append(v)\n    return visited.get("cx_start")\n\ndef _iso_week_start(d):\n    return d - timedelta(days=d.weekday())\n\ndef _to_date(v):\n    if v is None:\n        return None\n    if isinstance(v, date):\n        return v\n    try:\n        return date.fromisoformat(str(v)[:10])\n    except Exception:\n        return None\n\ndef build_plan(target_sites, window_days=60, prereq_threshold=0.80,\n               project_type="NTM", sla_dag=None, execute_query=None):\n    assert sla_dag is not None, "sla_dag must be passed (from kpi_sla_dag node property)"\n    assert execute_query is not None, "execute_query helper must be passed in"\n    pt = sla_dag[project_type]\n    today = date.today()\n    horizon = today + timedelta(days=window_days)\n\n    # --- 1. SINGLE SQL FETCH of in-flight sites -----------------------------\n    cols = set()\n    for spec in pt.values():\n        if spec.get("column"):     cols.add(spec["column"])\n        if spec.get("alt_column"): cols.add(spec["alt_column"])\n    cols |= {"pj_p_4225_construction_start_finish", "ms_1550_construction_start_actual",\n             "ms_1555_construction_complete_actual", "scoping_package_crane_required",\n             "s_site_id", "smp_name"}\n    col_list = ", ".join(sorted(c for c in cols if c))\n    smp_filter = ""\n    if project_type == "AHLOB":\n        smp_filter = " AND smp_name ILIKE \'%AHLOB%\'"\n    elif project_type == "NTM":\n        smp_filter = " AND (smp_name IS NULL OR smp_name NOT ILIKE \'%AHLOB%\')"\n    fetch_sql = (\n        "SELECT " + col_list +\n        " FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined" +\n        " WHERE ms_1550_construction_start_actual IS NULL" +\n        " AND s_site_id IS NOT NULL" +\n        smp_filter\n    )\n    rows = execute_query(fetch_sql) or []\n\n    # --- 2. GC RUN-RATE CAPACITY --------------------------------------------\n    cap_sql = (\n        "SELECT COUNT(DISTINCT s_site_id) AS cnt"\n        " FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined"\n        " WHERE ms_1555_construction_complete_actual::date BETWEEN "\n        "\'" + (today - timedelta(days=60)).isoformat() + "\'"\n        " AND \'" + today.isoformat() + "\'"\n    )\n    cap_rows = execute_query(cap_sql) or [{"cnt": 0}]\n    completed_last_60d = int(cap_rows[0].get("cnt") or 0)\n    weekly_cap = max(1, completed_last_60d // 8)   # GC run rate per week\n\n    # --- 3. PER-SITE COMPUTE -------------------------------------------------\n    committed, pull_forward = [], []\n    for row in rows:\n        # Normalize dates on every milestone column (DB may return strings)\n        for k, v in list(row.items()):\n            if k != "scoping_package_crane_required" and k != "smp_name" and k != "s_site_id":\n                row[k] = _to_date(v)\n        planned_cx = row.get("pj_p_4225_construction_start_finish")\n        if planned_cx is None:\n            continue\n        applicable = _applicable_nodes(pt, row)\n        done = {n for n in applicable if _is_done(n, pt[n], row)}\n        prereq_pct = (len(done) / len(applicable)) if applicable else 0.0\n        last_ms, last_dt = _last_completed_milestone(applicable, pt, row)\n        remaining = _shortest_path_days(pt, last_ms, applicable)\n        forecast = (last_dt + timedelta(days=remaining)) if (last_dt and remaining is not None) else None\n\n        if today <= planned_cx <= horizon:\n            committed.append({"site_id": row["s_site_id"], "planned_cx": planned_cx,\n                              "prereq_pct": round(prereq_pct, 2)})\n        elif planned_cx > horizon and prereq_pct >= prereq_threshold:\n            blockers = sorted(applicable - done)\n            pull_forward.append({"site_id": row["s_site_id"], "planned_cx": planned_cx,\n                                 "forecast_cx_ready": forecast,\n                                 "prereq_pct": round(prereq_pct, 2),\n                                 "last_milestone": last_ms,\n                                 "remaining_sla_days": remaining,\n                                 "blockers": blockers})\n\n    # --- 4. WEEKLY BUCKETS ---------------------------------------------------\n    buckets = defaultdict(lambda: {"committed": 0, "pull_forward": 0})\n    for s in committed:\n        buckets[_iso_week_start(s["planned_cx"])]["committed"] += 1\n    for s in pull_forward:\n        if s["forecast_cx_ready"]:\n            buckets[_iso_week_start(s["forecast_cx_ready"])]["pull_forward"] += 1\n    weekly_buckets = []\n    for ws in sorted(buckets):\n        b = buckets[ws]\n        total = b["committed"] + b["pull_forward"]\n        weekly_buckets.append({"week_start": ws, "committed": b["committed"],\n                               "pull_forward": b["pull_forward"], "total": total,\n                               "capacity_cap": weekly_cap, "over_capacity": total > weekly_cap})\n\n    return {\n        "summary": {"target": target_sites, "window_days": window_days,\n                    "committed_count": len(committed),\n                    "pull_forward_count": len(pull_forward),\n                    "total_in_window": len(committed) + len(pull_forward),\n                    "gap_vs_target": target_sites - (len(committed) + len(pull_forward))},\n        "weekly_buckets": weekly_buckets,\n        "capacity": {"method": "gc_run_rate",\n                     "completed_last_60d": completed_last_60d,\n                     "weekly_cap": weekly_cap},\n        "pull_forward_sites": pull_forward,\n        "config": {"prereq_threshold": prereq_threshold, "project_type": project_type},\n    }\n'});

# MATCH (a:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'cpf-001-construction-plan-forecast'}), (b:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'project'}) CREATE (a)-[:RELATES_TO {relationship_type: 'DERIVED_FROM', edge_id: 'cpf-rel-001-derives-project', session_id: '69a3d22f26e208edc083a06e', style: 'solid', relationship: 'derived_from', status: 'confirmed'}]->(b);
# MATCH (a:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'cpf-001-construction-plan-forecast'}), (b:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'site'}) CREATE (a)-[:RELATES_TO {relationship_type: 'DERIVED_FROM', edge_id: 'cpf-rel-002-derives-site', session_id: '69a3d22f26e208edc083a06e', style: 'solid', relationship: 'derived_from', status: 'confirmed'}]->(b);
# MATCH (a:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'cpf-001-construction-plan-forecast'}), (b:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'dcf98a0e-2891-47a3-a258-ebea62201ba4'}) CREATE (a)-[:RELATES_TO {relationship_type: 'BUILDS_ON', edge_id: 'cpf-rel-003-builds-on-prereq-readiness', session_id: '69a3d22f26e208edc083a06e', style: 'solid', relationship: 'builds_on', status: 'confirmed'}]->(b);
