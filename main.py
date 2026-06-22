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


# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'cc575a78-fe9f-4cad-b364-75e3f32f2c04'})
# SET
#     n.definition = 'Material Hold Time by GC — measures how long each general contractor is CURRENTLY holding material that has not yet reached the construction/swap milestone. By default measures hold until construction/swap start; the phase filter switches the anchor to construction/swap complete when the user asks about completion. For each region / area / market / GC / delay reason, reports the project count, average and maximum days held to date (computed from MSL pickup to today, since the milestone has not been reached), and the 8-day SLA breach counts. Helps identify which contractors are sitting on material the longest.',
#     n.kpi_business_logic = '1) Source: pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined; no JOIN. 2) Phase filter selects anchor + delay-code column: phase=\'start\' (default) → anchor = COALESCE(pj_a_4225_construction_start_finish, ms_1550_construction_start_actual), delay_reason from pj_construction_start_delay_code; phase=\'complete\' → anchor = COALESCE(pj_a_5175_construction_complete_finish, ms_1555_construction_complete_actual), delay_reason from pj_construction_complete_delay_code. 3) Population (STILL holding): pj_a_3925_msl_pickup_date_finish IS NOT NULL AND <anchor> IS NULL (milestone has not happened yet). 4) Apply optional equality filters: rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, smp_id, por_category. 5) smp_name conditional: if smp_name = \'NTM\', enforce smp_status = \'Active\' and exclude ntm_project_type IN (\'E2E06 - NSD Cx Mgmt\', \'E2E07 - OL/MOD Cx Mgmt\'). 6) Optional date range: start_date / end_date apply to pj_a_3925_msl_pickup_date_finish::date. 7) CTE base: per-project hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date (since anchor IS NULL, we use today as the upper bound). 8) Aggregate per (rgn_region, m_area, m_market, construction_gc, delay_reason): project_count = COUNT(DISTINCT pj_project_id); avg_hold_days = ROUND(AVG(hold_days)::numeric, 0); max_hold_days = MAX(hold_days); sla_breach_count = COUNT(DISTINCT projects where hold_days > 8); sla_breach_pct = ROUND(100.0 * breach / NULLIF(project_count, 0), 2). 9) ORDER BY avg_hold_days DESC NULLS LAST, construction_gc, delay_reason.',
#     n.kpi_contract = '{"function_name": "get_material_hold_time_by_gc", "node_type": "kpi", "node_id": "cc575a78-fe9f-4cad-b364-75e3f32f2c04", "node_label": "Material Hold Time by GC", "description": "Material Hold Time by GC \\u2014 measures how long each general contractor is CURRENTLY holding material that has not yet reached the construction/swap milestone. By default measures hold until construction/swap start; the phase filter switches the anchor to construction/swap complete when the user asks about completion. For each region / area / market / GC / delay reason, reports the project count, average and maximum days held to date (computed from MSL pickup to today, since the milestone has not been reached), and the 8-day SLA breach counts. Helps identify which contractors are sitting on material the longest.", "parameters": [{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values.", "required": false, "sample_values": ["NTM", "AHLOB Modernization"]}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id.", "required": false, "sample_values": []}, {"name": "phase", "type": "string", "description": "Which milestone to measure hold against. Default \'start\' (uses pj_a_4225/ms_1550 anchor + pj_construction_start_delay_code). \'complete\' uses pj_a_5175/ms_1555 + pj_construction_complete_delay_code. Population is always restricted to projects whose chosen milestone IS NULL (still holding).", "required": false, "sample_values": ["start", "complete"]}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_3925_msl_pickup_date_finish::date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_3925_msl_pickup_date_finish::date.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": false}, {"name": "m_area", "type": "string", "nullable": false}, {"name": "m_market", "type": "string", "nullable": false}, {"name": "construction_gc", "type": "string", "nullable": false}, {"name": "delay_reason", "type": "string", "nullable": true}, {"name": "project_count", "type": "number", "nullable": false}, {"name": "avg_hold_days", "type": "number", "nullable": true}, {"name": "max_hold_days", "type": "number", "nullable": true}, {"name": "sla_breach_count", "type": "number", "nullable": false}, {"name": "sla_breach_pct", "type": "number", "nullable": true}], "sample_output": [], "row_count": 50}',
#     n.kpi_description = 'Material Hold Time by GC — measures how long each general contractor is CURRENTLY holding material that has not yet reached the construction/swap milestone. By default measures hold until construction/swap start; the phase filter switches the anchor to construction/swap complete when the user asks about completion. For each region / area / market / GC / delay reason, reports the project count, average and maximum days held to date (computed from MSL pickup to today, since the milestone has not been reached), and the 8-day SLA breach counts. Helps identify which contractors are sitting on material the longest.',
#     n.kpi_filters = '[{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region."}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area."}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market."}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc."}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id."}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id."}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code."}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category."}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values."}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id."}, {"name": "phase", "type": "string", "description": "Which milestone to measure hold against. Default \'start\' (uses pj_a_4225/ms_1550 anchor + pj_construction_start_delay_code). \'complete\' uses pj_a_5175/ms_1555 + pj_construction_complete_delay_code. Population is always restricted to projects whose chosen milestone IS NULL (still holding)."}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_3925_msl_pickup_date_finish::date."}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_3925_msl_pickup_date_finish::date."}]',
#     n.kpi_formula_description = 'Per (rgn_region, m_area, m_market, construction_gc, delay_reason): hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date (measured for projects where the chosen anchor IS NULL, i.e., still holding); project_count = COUNT(DISTINCT pj_project_id); avg_hold_days = ROUND(AVG(hold_days)::numeric, 0); max_hold_days = MAX(hold_days); sla_breach_count = COUNT(DISTINCT pj_project_id WHERE hold_days > 8); sla_breach_pct = ROUND(100.0 * sla_breach_count / NULLIF(project_count, 0), 2). Phase filter: \'start\' (default) uses pj_a_4225/ms_1550 + pj_construction_start_delay_code; \'complete\' uses pj_a_5175/ms_1555 + pj_construction_complete_delay_code.',
#     n.kpi_output_schema = '[{"column": "rgn_region", "type": "string", "description": "Region grouping field."}, {"column": "m_area", "type": "string", "description": "Area grouping field."}, {"column": "m_market", "type": "string", "description": "Market grouping field."}, {"column": "construction_gc", "type": "string", "description": "General contractor (focal grouping)."}, {"column": "delay_reason", "type": "string", "description": "Delay reason from the phase-specific delay-code column; NULL when no reason recorded."}, {"column": "project_count", "type": "int", "description": "COUNT(DISTINCT pj_project_id) of projects still holding material in this cell."}, {"column": "avg_hold_days", "type": "number", "description": "ROUND(AVG(hold_days)::numeric, 0) \\u2014 average days held from MSL pickup to today."}, {"column": "max_hold_days", "type": "number", "description": "Longest hold (days) in the cell."}, {"column": "sla_breach_count", "type": "int", "description": "Projects with hold_days > 8 (SLA threshold)."}, {"column": "sla_breach_pct", "type": "number", "description": "100 * sla_breach_count / project_count, rounded to 2 dp."}]',
#     n.kpi_python_function = 'def get_material_hold_time_by_gc(execute_query, filters=None) -> list[dict]:\n    """Material Hold Time by GC — still-holding view per (geo, GC, delay reason).\n\n    Population: projects whose MSL pickup has happened (pj_a_3925_msl_pickup_date_finish IS NOT NULL)\n    AND the chosen milestone has NOT happened yet (anchor IS NULL — they are still holding material).\n    hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date.\n    Counts use COUNT(DISTINCT pj_project_id).\n\n    The `phase` filter selects which milestone we are measuring hold against:\n      - \'start\'    (default) → anchor = COALESCE(pj_a_4225_construction_start_finish,\n                                                  ms_1550_construction_start_actual)\n                               delay_reason from pj_construction_start_delay_code\n      - \'complete\'           → anchor = COALESCE(pj_a_5175_construction_complete_finish,\n                                                  ms_1555_construction_complete_actual)\n                               delay_reason from pj_construction_complete_delay_code\n\n    Optional generic equality filters: rgn_region, m_area, m_market, construction_gc,\n    pj_project_id, s_site_id, customer_site_code, smp_name, smp_id, por_category.\n    smp_name = \'NTM\' → also enforces smp_status=\'Active\' and excludes specific ntm_project_type values.\n    Optional start_date / end_date apply to pj_a_3925_msl_pickup_date_finish::date.\n\n    Output per row:\n      project_count, avg_hold_days (ROUND to 0), max_hold_days, sla_breach_count, sla_breach_pct.\n    """\n    filters = filters or {}\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    where_parts = []\n\n    optional_eq_cols = [\n        "rgn_region", "m_area", "m_market", "construction_gc",\n        "pj_project_id", "s_site_id", "customer_site_code",\n        "smp_id", "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            where_parts.append(f\'"{col}" = \\\'{_esc(v)}\\\'\')\n\n    smp_name = filters.get("smp_name")\n    if smp_name is not None and str(smp_name).strip() != "":\n        v = _esc(smp_name)\n        where_parts.append(f\'"smp_name" = \\\'{v}\\\'\')\n        if str(smp_name).strip().upper() == \'NTM\':\n            where_parts.append(\'"smp_status" = \\\'Active\\\'\')\n            where_parts.append(\'"ntm_project_type" NOT IN (\\\'E2E06 - NSD Cx Mgmt\\\', \\\'E2E07 - OL/MOD Cx Mgmt\\\')\')\n\n    # Phase: \'start\' (default) or \'complete\' — drives which anchor + delay-code column are used\n    phase_raw = filters.get("phase")\n    phase = (str(phase_raw).strip().lower() if phase_raw is not None and str(phase_raw).strip() != "" else "start")\n    if phase not in ("start", "complete"):\n        phase = "start"\n\n    if phase == "start":\n        anchor_expr = \'COALESCE("pj_a_4225_construction_start_finish", "ms_1550_construction_start_actual")\'\n        delay_col = "pj_construction_start_delay_code"\n    else:\n        anchor_expr = \'COALESCE("pj_a_5175_construction_complete_finish", "ms_1555_construction_complete_actual")\'\n        delay_col = "pj_construction_complete_delay_code"\n\n    # Population: STILL holding — MSL pickup happened, milestone has NOT happened yet\n    where_parts.append(\'"pj_a_3925_msl_pickup_date_finish" IS NOT NULL\')\n    where_parts.append(f\'{anchor_expr} IS NULL\')\n\n    start_date = filters.get("start_date")\n    end_date = filters.get("end_date")\n    if start_date:\n        where_parts.append(f\'"pj_a_3925_msl_pickup_date_finish"::date >= DATE \\\'{_esc(start_date)}\\\'\')\n    if end_date:\n        where_parts.append(f\'"pj_a_3925_msl_pickup_date_finish"::date <= DATE \\\'{_esc(end_date)}\\\'\')\n\n    where_sql = " AND ".join(where_parts)\n\n    sla_days = 8\n\n    sql = f"""\n        WITH base AS (\n            SELECT\n                "rgn_region",\n                "m_area",\n                "m_market",\n                "construction_gc",\n                "{delay_col}" AS delay_reason,\n                "pj_project_id",\n                (CURRENT_DATE - "pj_a_3925_msl_pickup_date_finish"::date) AS hold_days\n            FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined\n            WHERE {where_sql}\n        )\n        SELECT\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc",\n            delay_reason,\n            COUNT(DISTINCT "pj_project_id") AS project_count,\n            ROUND(AVG(hold_days)::numeric, 0) AS avg_hold_days,\n            MAX(hold_days) AS max_hold_days,\n            COUNT(DISTINCT CASE WHEN hold_days > {sla_days} THEN "pj_project_id" END) AS sla_breach_count,\n            ROUND(\n                100.0 * COUNT(DISTINCT CASE WHEN hold_days > {sla_days} THEN "pj_project_id" END)\n                / NULLIF(COUNT(DISTINCT "pj_project_id"), 0),\n                2\n            ) AS sla_breach_pct\n        FROM base\n        GROUP BY "rgn_region", "m_area", "m_market", "construction_gc", delay_reason\n        ORDER BY avg_hold_days DESC NULLS LAST, "construction_gc", delay_reason\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_columns = ['stg_ndpd_mbt_tmobile_macro_combined.pj_project_id', 'stg_ndpd_mbt_tmobile_macro_combined.s_site_id', 'stg_ndpd_mbt_tmobile_macro_combined.customer_site_code', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.smp_status', 'stg_ndpd_mbt_tmobile_macro_combined.ntm_project_type', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc', 'stg_ndpd_mbt_tmobile_macro_combined.por_category', 'stg_ndpd_mbt_tmobile_macro_combined.pj_a_3925_msl_pickup_date_finish', 'stg_ndpd_mbt_tmobile_macro_combined.pj_a_4225_construction_start_finish', 'stg_ndpd_mbt_tmobile_macro_combined.ms_1550_construction_start_actual', 'stg_ndpd_mbt_tmobile_macro_combined.pj_a_5175_construction_complete_finish', 'stg_ndpd_mbt_tmobile_macro_combined.ms_1555_construction_complete_actual', 'stg_ndpd_mbt_tmobile_macro_combined.pj_construction_start_delay_code', 'stg_ndpd_mbt_tmobile_macro_combined.pj_construction_complete_delay_code'],
#     n.nl_description = 'Material Hold Time by GC — measures how long each general contractor is CURRENTLY holding material that has not yet reached the construction/swap milestone. By default measures hold until construction/swap start; the phase filter switches the anchor to construction/swap complete when the user asks about completion. For each region / area / market / GC / delay reason, reports the project count, average and maximum days held to date (computed from MSL pickup to today, since the milestone has not been reached), and the 8-day SLA breach counts. Helps identify which contractors are sitting on material the longest.'
# RETURN n.label AS label, n.node_id AS node_id;