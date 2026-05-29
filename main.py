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


# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: '848a393e-28b7-4b4b-b099-dbd60c208ddf'})
# SET
#     n.kpi_python_function = 'def get_forecast_accuracy_pct(execute_query, filters=None) -> list[dict]:\n    """Forecast Accuracy % — original-plan-vs-actual hit rate per geo, week granularity.\n\n    Compares the ORIGINAL forecast date for each project (MIN over the forecast table) to the\n    ACTUAL construction start date (pj_a_4225_construction_start_finish). A project is flagged\n    accurate when date_trunc(\'week\', initial_forecast_date) = date_trunc(\'week\', actual_start_date).\n    Output: per-geo ROUND(AVG(is_accurate) * 100, 2) plus raw counts.\n\n    Population:\n      - pj_a_4225_construction_start_finish IS NOT NULL\n      - smp_name = \'NTM\' → also enforces smp_status = \'Active\' and excludes\n        ntm_project_type IN (\'E2E06 - NSD Cx Mgmt\', \'E2E07 - OL/MOD Cx Mgmt\')\n\n    Optional generic equality filters:\n      rgn_region, m_area, m_market, construction_gc,\n      pj_project_id, s_site_id, customer_site_code,\n      smp_name, smp_id, por_category.\n\n    Optional delay filter:\n      pj_construction_start_delay_code — when provided, narrows to delayed projects (column\n      IS NOT NULL AND <> \'No Delay\'). Sentinel values ("any","true","yes","1","*") restrict to\n      *any* delay; any other value applies an exact equality filter on the delay code.\n\n    Optional date range: start_date / end_date applied to pj_a_4225_construction_start_finish::date.\n    """\n    filters = filters or {}\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    where_parts = [\'c."pj_a_4225_construction_start_finish" IS NOT NULL\']\n\n    optional_eq_cols = [\n        "rgn_region", "m_area", "m_market", "construction_gc",\n        "pj_project_id", "s_site_id", "customer_site_code",\n        "smp_id", "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            where_parts.append(f\'c."{col}" = \\\'{_esc(v)}\\\'\')\n\n    smp_name = filters.get("smp_name")\n    if smp_name is not None and str(smp_name).strip() != "":\n        v = _esc(smp_name)\n        where_parts.append(f\'c."smp_name" = \\\'{v}\\\'\')\n        if str(smp_name).strip().upper() == \'NTM\':\n            where_parts.append(\'c."smp_status" = \\\'Active\\\'\')\n            where_parts.append(\'c."ntm_project_type" NOT IN (\\\'E2E06 - NSD Cx Mgmt\\\', \\\'E2E07 - OL/MOD Cx Mgmt\\\')\')\n\n    # Optional construction-start delay-code filter (engages when caller wants delayed projects)\n    _v = filters.get("pj_construction_start_delay_code")\n    if _v is not None and str(_v).strip() != "":\n        _s = str(_v).strip()\n        where_parts.append(\'c."pj_construction_start_delay_code" IS NOT NULL\')\n        where_parts.append(\'c."pj_construction_start_delay_code" <> \\\'No Delay\\\'\')\n        if _s.lower() not in ("any", "true", "yes", "1", "*"):\n            _ss = _s.replace("\'", "\'\'")\n            where_parts.append(f\'c."pj_construction_start_delay_code" = \\\'{_ss}\\\'\')\n\n    start_date = filters.get("start_date")\n    end_date = filters.get("end_date")\n    if start_date:\n        where_parts.append(f\'c."pj_a_4225_construction_start_finish"::date >= DATE \\\'{_esc(start_date)}\\\'\')\n    if end_date:\n        where_parts.append(f\'c."pj_a_4225_construction_start_finish"::date <= DATE \\\'{_esc(end_date)}\\\'\')\n\n    where_sql = " AND ".join(where_parts)\n\n    sql = f"""\n        WITH initial_forecast AS (\n            SELECT pj_project_id, MIN(construction_start_finish) AS initial_forecast_date\n            FROM public.tmo_macro_copilot_short_construction_start_forecast\n            GROUP BY pj_project_id\n        ),\n        actual AS (\n            SELECT\n                c."pj_project_id",\n                c."rgn_region",\n                c."m_area",\n                c."m_market",\n                c."construction_gc",\n                c."pj_a_4225_construction_start_finish" AS actual_start_date\n            FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined c\n            WHERE {where_sql}\n        ),\n        base AS (\n            SELECT\n                a."rgn_region",\n                a."m_area",\n                a."m_market",\n                a."construction_gc",\n                CASE\n                    WHEN date_trunc(\'week\', i.initial_forecast_date::timestamp)\n                       = date_trunc(\'week\', a.actual_start_date::timestamp)\n                    THEN 1 ELSE 0\n                END AS is_accurate\n            FROM initial_forecast i\n            JOIN actual a ON i.pj_project_id = a."pj_project_id"\n        )\n        SELECT\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc",\n            COUNT(*) AS project_count,\n            SUM(is_accurate) AS accurate_count,\n            ROUND(AVG(is_accurate::numeric) * 100, 2) AS forecast_accuracy_week_pct\n        FROM base\n        GROUP BY "rgn_region", "m_area", "m_market", "construction_gc"\n        ORDER BY "rgn_region", "m_area", "m_market", "construction_gc"\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_columns = ['stg_ndpd_mbt_tmobile_macro_combined.pj_project_id', 'stg_ndpd_mbt_tmobile_macro_combined.s_site_id', 'stg_ndpd_mbt_tmobile_macro_combined.customer_site_code', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.smp_status', 'stg_ndpd_mbt_tmobile_macro_combined.ntm_project_type', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc', 'stg_ndpd_mbt_tmobile_macro_combined.por_category', 'stg_ndpd_mbt_tmobile_macro_combined.pj_a_4225_construction_start_finish', 'stg_ndpd_mbt_tmobile_macro_combined.pj_construction_start_delay_code', 'tmo_macro_copilot_short_construction_start_forecast.pj_project_id', 'tmo_macro_copilot_short_construction_start_forecast.construction_start_finish'],
#     n.kpi_filters = '[{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region."}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area."}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market."}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc."}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id."}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id."}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code."}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category."}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values."}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id."}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_4225_construction_start_finish::date."}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_4225_construction_start_finish::date."}, {"name": "pj_construction_start_delay_code", "type": "string", "description": "Optional filter for delayed projects. Sentinel values (\'any\',\'true\',\'yes\',\'1\',\'*\') restrict to ANY non-empty delay code (IS NOT NULL AND <> \'No Delay\'). Any other value applies an exact equality on the delay code in addition to the IS NOT NULL guards."}]',
#     n.kpi_contract = '{"function_name": "get_forecast_accuracy_pct", "node_type": "kpi", "node_id": "848a393e-28b7-4b4b-b099-dbd60c208ddf", "node_label": "Forecast Accuracy %", "description": "Forecast Accuracy % \\u2014 share of projects whose ACTUAL construction start landed in the same ISO week as the ORIGINAL forecast date (MIN over the forecast table per project). Reported as ROUND(AVG(is_accurate) * 100, 2) per (rgn_region, m_area, m_market, construction_gc), with supporting project_count and accurate_count.", "parameters": [{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values.", "required": false, "sample_values": []}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id.", "required": false, "sample_values": []}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_4225_construction_start_finish::date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_4225_construction_start_finish::date.", "required": false, "sample_values": []}, {"name": "pj_construction_start_delay_code", "type": "string", "description": "Optional filter for delayed projects. Sentinel values (\'any\',\'true\',\'yes\',\'1\',\'*\') restrict to ANY non-empty delay code (IS NOT NULL AND <> \'No Delay\'). Any other value applies an exact equality on the delay code in addition to the IS NOT NULL guards.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": true}, {"name": "m_area", "type": "string", "nullable": true}, {"name": "m_market", "type": "string", "nullable": true}, {"name": "construction_gc", "type": "string", "nullable": true}, {"name": "project_count", "type": "number", "nullable": true}, {"name": "accurate_count", "type": "number", "nullable": true}, {"name": "forecast_accuracy_week_pct", "type": "number", "nullable": true}], "sample_output": [], "row_count": 10}',
#     n.kpi_business_logic = '1) Source: macro_combined (c) for actuals and dims; public.tmo_macro_copilot_short_construction_start_forecast (f) for historical forecast dates. 2) Population: c.pj_a_4225_construction_start_finish IS NOT NULL. 3) Apply optional equality filters on c.*: rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, smp_id, por_category. 4) smp_name conditional: when \'NTM\', also enforce c.smp_status=\'Active\' and exclude c.ntm_project_type IN (\'E2E06 - NSD Cx Mgmt\', \'E2E07 - OL/MOD Cx Mgmt\'). 5) Optional date range: start_date / end_date applied to c.pj_a_4225_construction_start_finish::date. 6) CTE initial_forecast: per project MIN(forecast.construction_start_finish) AS initial_forecast_date. 7) CTE actual: filtered macro_combined rows projecting geo + actual_start_date = pj_a_4225_construction_start_finish. 8) CTE base: INNER JOIN of initial_forecast and actual on pj_project_id; is_accurate = 1 when date_trunc(\'week\', initial_forecast_date) = date_trunc(\'week\', actual_start_date), else 0. 9) Aggregate per (rgn_region, m_area, m_market, construction_gc): project_count = COUNT(*), accurate_count = SUM(is_accurate), forecast_accuracy_week_pct = ROUND(AVG(is_accurate::numeric) * 100, 2). Optional pj_construction_start_delay_code filter (when provided) adds c."pj_construction_start_delay_code" IS NOT NULL AND <> \'No Delay\'; non-sentinel values also add equality c."pj_construction_start_delay_code" = \'<value>\'.'
# RETURN n.label AS label, n.node_id AS node_id;


// ----- Site Reschedule Count (277ecc42-c37c-46f9-9d9c-6c2e8d672a63) -----
# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: '277ecc42-c37c-46f9-9d9c-6c2e8d672a63'})
# SET
#     n.kpi_python_function = 'def get_site_reschedule_count(execute_query, filters=None) -> list[dict]:\n    """Site Reschedule Count — per-project count of construction-start reschedules.\n\n    Reschedule count = GREATEST(COUNT(forecast rows for that project) - 1, 0). When the project\n    has been rescheduled N times, the forecast table holds N + 1 historical planned dates; the\n    clamp protects the LEFT-JOIN null edge case where no forecast row exists.\n\n    Population:\n      - pj_a_4225_construction_start_finish IS NOT NULL (only projects that actually started)\n      - smp_name = \'NTM\' → also enforces smp_status = \'Active\' and excludes\n        ntm_project_type IN (\'E2E06 - NSD Cx Mgmt\', \'E2E07 - OL/MOD Cx Mgmt\')\n\n    Optional generic equality filters:\n      rgn_region, m_area, m_market, construction_gc,\n      pj_project_id, s_site_id, customer_site_code,\n      smp_name, smp_id, por_category.\n\n    Optional delay filter:\n      pj_construction_start_delay_code — when provided, narrows to delayed projects (column\n      IS NOT NULL AND <> \'No Delay\'). Sentinel values ("any","true","yes","1","*") restrict to\n      *any* delay; any other value applies an exact equality filter on the delay code.\n\n    Optional date range: start_date / end_date applied to pj_a_4225_construction_start_finish::date.\n\n    Output: one row per project (pj_project_id, rgn_region, m_area, m_market, construction_gc,\n    rescheduled_count), ordered by rescheduled_count DESC.\n    """\n    filters = filters or {}\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    where_parts = [\'c."pj_a_4225_construction_start_finish" IS NOT NULL\']\n\n    optional_eq_cols = [\n        "rgn_region", "m_area", "m_market", "construction_gc",\n        "pj_project_id", "s_site_id", "customer_site_code",\n        "smp_id", "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            where_parts.append(f\'c."{col}" = \\\'{_esc(v)}\\\'\')\n\n    smp_name = filters.get("smp_name")\n    if smp_name is not None and str(smp_name).strip() != "":\n        v = _esc(smp_name)\n        where_parts.append(f\'c."smp_name" = \\\'{v}\\\'\')\n        if str(smp_name).strip().upper() == \'NTM\':\n            where_parts.append(\'c."smp_status" = \\\'Active\\\'\')\n            where_parts.append(\'c."ntm_project_type" NOT IN (\\\'E2E06 - NSD Cx Mgmt\\\', \\\'E2E07 - OL/MOD Cx Mgmt\\\')\')\n\n    # Optional construction-start delay-code filter (engages when caller wants delayed projects)\n    _v = filters.get("pj_construction_start_delay_code")\n    if _v is not None and str(_v).strip() != "":\n        _s = str(_v).strip()\n        where_parts.append(\'c."pj_construction_start_delay_code" IS NOT NULL\')\n        where_parts.append(\'c."pj_construction_start_delay_code" <> \\\'No Delay\\\'\')\n        if _s.lower() not in ("any", "true", "yes", "1", "*"):\n            _ss = _s.replace("\'", "\'\'")\n            where_parts.append(f\'c."pj_construction_start_delay_code" = \\\'{_ss}\\\'\')\n\n    start_date = filters.get("start_date")\n    end_date = filters.get("end_date")\n    if start_date:\n        where_parts.append(f\'c."pj_a_4225_construction_start_finish"::date >= DATE \\\'{_esc(start_date)}\\\'\')\n    if end_date:\n        where_parts.append(f\'c."pj_a_4225_construction_start_finish"::date <= DATE \\\'{_esc(end_date)}\\\'\')\n\n    where_sql = " AND ".join(where_parts)\n\n    sql = f"""\n        SELECT\n            c."pj_project_id",\n            c."rgn_region",\n            c."m_area",\n            c."m_market",\n            c."construction_gc",\n            GREATEST(COUNT(f.pj_project_id) - 1, 0) AS rescheduled_count\n        FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined c\n        LEFT JOIN public.tmo_macro_copilot_short_construction_start_forecast f\n            ON c."pj_project_id" = f.pj_project_id\n        WHERE {where_sql}\n        GROUP BY\n            c."pj_project_id",\n            c."rgn_region",\n            c."m_area",\n            c."m_market",\n            c."construction_gc"\n        ORDER BY rescheduled_count DESC, c."pj_project_id"\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_columns = ['stg_ndpd_mbt_tmobile_macro_combined.pj_project_id', 'stg_ndpd_mbt_tmobile_macro_combined.s_site_id', 'stg_ndpd_mbt_tmobile_macro_combined.customer_site_code', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.smp_status', 'stg_ndpd_mbt_tmobile_macro_combined.ntm_project_type', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc', 'stg_ndpd_mbt_tmobile_macro_combined.por_category', 'stg_ndpd_mbt_tmobile_macro_combined.pj_a_4225_construction_start_finish', 'stg_ndpd_mbt_tmobile_macro_combined.pj_construction_start_delay_code', 'tmo_macro_copilot_short_construction_start_forecast.pj_project_id', 'tmo_macro_copilot_short_construction_start_forecast.construction_start_finish'],
#     n.kpi_filters = '[{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region."}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area."}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market."}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc."}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id."}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id."}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code."}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category."}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values."}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id."}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_4225_construction_start_finish::date."}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_4225_construction_start_finish::date."}, {"name": "pj_construction_start_delay_code", "type": "string", "description": "Optional filter for delayed projects. Sentinel values (\'any\',\'true\',\'yes\',\'1\',\'*\') restrict to ANY non-empty delay code (IS NOT NULL AND <> \'No Delay\'). Any other value applies an exact equality on the delay code in addition to the IS NOT NULL guards."}]',
#     n.kpi_contract = '{"function_name": "get_site_reschedule_count", "node_type": "kpi", "node_id": "277ecc42-c37c-46f9-9d9c-6c2e8d672a63", "node_label": "Site Reschedule Count", "description": "Site Reschedule Count \\u2014 per-project number of times the planned construction start date was overwritten before construction began, computed as GREATEST(COUNT(forecast rows) - 1, 0). Returns one row per project with geo context and rescheduled_count.", "parameters": [{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values.", "required": false, "sample_values": []}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id.", "required": false, "sample_values": []}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_4225_construction_start_finish::date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_4225_construction_start_finish::date.", "required": false, "sample_values": []}, {"name": "pj_construction_start_delay_code", "type": "string", "description": "Optional filter for delayed projects. Sentinel values (\'any\',\'true\',\'yes\',\'1\',\'*\') restrict to ANY non-empty delay code (IS NOT NULL AND <> \'No Delay\'). Any other value applies an exact equality on the delay code in addition to the IS NOT NULL guards.", "required": false, "sample_values": []}], "output_columns": [{"name": "pj_project_id", "type": "string", "nullable": false}, {"name": "rgn_region", "type": "string", "nullable": true}, {"name": "m_area", "type": "string", "nullable": true}, {"name": "m_market", "type": "string", "nullable": true}, {"name": "construction_gc", "type": "string", "nullable": true}, {"name": "rescheduled_count", "type": "number", "nullable": true}], "sample_output": [], "row_count": 100}',
#     n.kpi_business_logic = '1) Source: macro_combined (c) joined to public.tmo_macro_copilot_short_construction_start_forecast (f) via LEFT JOIN on c.pj_project_id = f.pj_project_id (LEFT keeps projects with zero forecast rows). 2) Population: c.pj_a_4225_construction_start_finish IS NOT NULL. 3) Apply optional equality filters on c.* (same surface as Forecast Accuracy %) and smp_name conditional. 4) Optional date range: start_date / end_date applied to c.pj_a_4225_construction_start_finish::date. 5) GROUP BY c.pj_project_id, c.rgn_region, c.m_area, c.m_market, c.construction_gc. 6) rescheduled_count = GREATEST(COUNT(f.pj_project_id) - 1, 0). 7) ORDER BY rescheduled_count DESC, c.pj_project_id. Optional pj_construction_start_delay_code filter (when provided) adds c."pj_construction_start_delay_code" IS NOT NULL AND <> \'No Delay\'; non-sentinel values also add equality c."pj_construction_start_delay_code" = \'<value>\'.'
# RETURN n.label AS label, n.node_id AS node_id;
