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

# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: '74904211-c4aa-48c0-a998-aaf0ab9d58cb'})
# SET
#     n.label = 'Swap/Construction Completion Rate',
#     n.kpi_name = 'Swap/Construction Completion Rate',
#     n.kpi_python_function = 'def get_swap_construction_completion_rate(execute_query, filters=None) -> list[dict]:\n    """Swap/Construction Completion Rate — per (geo, GC, delay reason).\n\n    actual_date  = pj_a_5175_construction_complete_finish\n    planned_date = pj_p_5175_construction_complete_finish\n    gap_days     = actual_date::date - planned_date::date  (when both present)\n\n    Population:\n      - Excludes Dead projects (COALESCE(pj_project_status, \'\') <> \'Dead\').\n      - smp_name = \'NTM\' → also enforces smp_status=\'Active\' and excludes specific\n        ntm_project_type values.\n\n    Optional generic equality filters: rgn_region, m_area, m_market, construction_gc,\n    pj_project_id, s_site_id, customer_site_code, smp_name, smp_id, por_category.\n\n    Optional date range: start_date / end_date apply to actual_date and restrict ONLY\n    the completed_count / completion_rate / gap / on-time counters; total_sites and\n    pending_count are NOT date-filtered so completion % stays meaningful.\n\n    Output (one row per (rgn_region, m_area, m_market, construction_gc, delay_reason)):\n      total_sites, completed_count, pending_count, completion_rate_pct,\n      avg_gap_days (ROUND to 0), max_gap_days, on_time_count, delayed_count, on_time_pct.\n    """\n    filters = filters or {}\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    where_parts = ["COALESCE(\\"pj_project_status\\", \'\') <> \'Dead\'"]\n\n    optional_eq_cols = [\n        "rgn_region", "m_area", "m_market", "construction_gc",\n        "pj_project_id", "s_site_id", "customer_site_code",\n        "smp_id", "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            where_parts.append(f\'"{col}" = \\\'{_esc(v)}\\\'\')\n\n    smp_name = filters.get("smp_name")\n    if smp_name is not None and str(smp_name).strip() != "":\n        v = _esc(smp_name)\n        where_parts.append(f\'"smp_name" = \\\'{v}\\\'\')\n        if str(smp_name).strip().upper() == \'NTM\':\n            where_parts.append(\'"smp_status" = \\\'Active\\\'\')\n            where_parts.append(\'"ntm_project_type" NOT IN (\\\'E2E06 - NSD Cx Mgmt\\\', \\\'E2E07 - OL/MOD Cx Mgmt\\\')\')\n\n    where_sql = " AND ".join(where_parts)\n\n    # Date range applies to actual_date — restricts only the "completed" measurements,\n    # keeping total_sites and pending_count unfiltered so the rate stays meaningful.\n    completed_extra = ""\n    start_date = filters.get("start_date")\n    end_date = filters.get("end_date")\n    if start_date:\n        completed_extra += f" AND actual_date::date >= DATE \'{_esc(start_date)}\'"\n    if end_date:\n        completed_extra += f" AND actual_date::date <= DATE \'{_esc(end_date)}\'"\n\n    sql = f"""\n        WITH base AS (\n            SELECT\n                "rgn_region",\n                "m_area",\n                "m_market",\n                "construction_gc",\n                "pj_construction_complete_delay_code" AS delay_reason,\n                "pj_project_id",\n                "pj_a_5175_construction_complete_finish" AS actual_date,\n                "pj_p_5175_construction_complete_finish" AS planned_date,\n                CASE\n                    WHEN "pj_a_5175_construction_complete_finish" IS NOT NULL\n                     AND "pj_p_5175_construction_complete_finish" IS NOT NULL\n                    THEN "pj_a_5175_construction_complete_finish"::date\n                       - "pj_p_5175_construction_complete_finish"::date\n                END AS gap_days\n            FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined\n            WHERE {where_sql}\n        )\n        SELECT\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc",\n            delay_reason,\n            COUNT(DISTINCT "pj_project_id") AS total_sites,\n            COUNT(DISTINCT CASE\n                WHEN actual_date IS NOT NULL{completed_extra}\n                THEN "pj_project_id"\n            END) AS completed_count,\n            COUNT(DISTINCT CASE\n                WHEN actual_date IS NULL\n                THEN "pj_project_id"\n            END) AS pending_count,\n            ROUND(\n                100.0 * COUNT(DISTINCT CASE\n                    WHEN actual_date IS NOT NULL{completed_extra}\n                    THEN "pj_project_id"\n                END) / NULLIF(COUNT(DISTINCT "pj_project_id"), 0),\n                2\n            ) AS completion_rate_pct,\n            ROUND(AVG(CASE WHEN gap_days IS NOT NULL{completed_extra} THEN gap_days END)::numeric, 0) AS avg_gap_days,\n            MAX(CASE WHEN gap_days IS NOT NULL{completed_extra} THEN gap_days END) AS max_gap_days,\n            COUNT(DISTINCT CASE\n                WHEN gap_days IS NOT NULL AND gap_days <= 0{completed_extra}\n                THEN "pj_project_id"\n            END) AS on_time_count,\n            COUNT(DISTINCT CASE\n                WHEN gap_days IS NOT NULL AND gap_days > 0{completed_extra}\n                THEN "pj_project_id"\n            END) AS delayed_count,\n            ROUND(\n                100.0 * COUNT(DISTINCT CASE\n                    WHEN gap_days IS NOT NULL AND gap_days <= 0{completed_extra}\n                    THEN "pj_project_id"\n                END) / NULLIF(COUNT(DISTINCT CASE\n                    WHEN gap_days IS NOT NULL{completed_extra}\n                    THEN "pj_project_id"\n                END), 0),\n                2\n            ) AS on_time_pct\n        FROM base\n        GROUP BY "rgn_region", "m_area", "m_market", "construction_gc", delay_reason\n        ORDER BY "rgn_region", "m_area", "m_market", "construction_gc", delay_reason\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_columns = ['pj_project_status', 'rgn_region', 'm_area', 'm_market', 'construction_gc', 'pj_project_id', 'por_category', 's_site_id', 'customer_site_code', 'smp_name', 'smp_status', 'smp_id', 'ntm_project_type', 'pj_a_5175_construction_complete_finish', 'pj_p_5175_construction_complete_finish', 'pj_construction_complete_delay_code'],
#     n.kpi_filters = '[{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region."}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area."}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market."}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc."}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id."}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id."}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code."}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category."}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values."}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id."}, {"name": "start_date", "type": "date", "description": "Lower bound on actual completion date (pj_a_5175). Applied only to completed/gap/on-time aggregates."}, {"name": "end_date", "type": "date", "description": "Upper bound on actual completion date (pj_a_5175). Applied only to completed/gap/on-time aggregates."}]',
#     n.kpi_contract = '{"function_name": "get_swap_construction_completion_rate", "node_type": "kpi", "node_id": "74904211-c4aa-48c0-a998-aaf0ab9d58cb", "node_label": "Swap/Construction Completion Rate", "description": "Swap/Construction Completion Rate \\u2014 measures, for each region / area / market / GC and delay reason, what share of AHLOB swaps or NTM constructions have been completed. Reports completed and pending counts, completion percentage, the average and maximum gap between planned and actual completion dates, and on-time vs delayed counts. Delay reasons surface why some completions slipped past plan. AHLOB and NTM share the same underlying milestone, so this single KPI covers both programs.", "parameters": [{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values.", "required": false, "sample_values": ["NTM", "AHLOB Modernization"]}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id.", "required": false, "sample_values": []}, {"name": "start_date", "type": "date", "description": "Lower bound on actual completion date (pj_a_5175). Applied only to completed/gap/on-time aggregates.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on actual completion date (pj_a_5175). Applied only to completed/gap/on-time aggregates.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": false}, {"name": "m_area", "type": "string", "nullable": false}, {"name": "m_market", "type": "string", "nullable": false}, {"name": "construction_gc", "type": "string", "nullable": false}, {"name": "delay_reason", "type": "string", "nullable": true}, {"name": "total_sites", "type": "number", "nullable": false}, {"name": "completed_count", "type": "number", "nullable": false}, {"name": "pending_count", "type": "number", "nullable": false}, {"name": "completion_rate_pct", "type": "number", "nullable": false}, {"name": "avg_gap_days", "type": "number", "nullable": true}, {"name": "max_gap_days", "type": "number", "nullable": true}, {"name": "on_time_count", "type": "number", "nullable": false}, {"name": "delayed_count", "type": "number", "nullable": false}, {"name": "on_time_pct", "type": "number", "nullable": true}], "sample_output": [], "row_count": 50}',
#     n.kpi_output_schema = '[{"column": "rgn_region", "type": "string", "description": "Region grouping field."}, {"column": "m_area", "type": "string", "description": "Area grouping field."}, {"column": "m_market", "type": "string", "description": "Market grouping field."}, {"column": "construction_gc", "type": "string", "description": "General contractor grouping field."}, {"column": "delay_reason", "type": "string", "description": "Delay reason from pj_construction_complete_delay_code; NULL when no reason recorded."}, {"column": "total_sites", "type": "int", "description": "Distinct projects in the cell (not \'Dead\')."}, {"column": "completed_count", "type": "int", "description": "Projects with actual completion date in the optional window."}, {"column": "pending_count", "type": "int", "description": "Projects with no actual completion date yet."}, {"column": "completion_rate_pct", "type": "number", "description": "ROUND(100 * completed_count / total_sites, 2)."}, {"column": "avg_gap_days", "type": "number", "description": "ROUND(AVG(actual - planned)::numeric, 0); positive = late, negative = early."}, {"column": "max_gap_days", "type": "number", "description": "Worst gap in the cell."}, {"column": "on_time_count", "type": "int", "description": "Completed projects with gap_days <= 0."}, {"column": "delayed_count", "type": "int", "description": "Completed projects with gap_days > 0."}, {"column": "on_time_pct", "type": "number", "description": "ROUND(100 * on_time_count / (on_time_count + delayed_count), 2)."}]',
#     n.kpi_business_logic = '1) Source: pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined; no JOIN. 2) Excludes Dead projects: COALESCE(pj_project_status, \'\') <> \'Dead\'. 3) Optional equality filters: rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, smp_id, por_category. 4) smp_name conditional: when \'NTM\', also enforce smp_status=\'Active\' and exclude ntm_project_type IN (\'E2E06 - NSD Cx Mgmt\', \'E2E07 - OL/MOD Cx Mgmt\'). 5) Optional date range (start_date / end_date) applies to pj_a_5175_construction_complete_finish and restricts ONLY the completed_count, completion_rate_pct, gap and on-time aggregates; total_sites and pending_count remain unfiltered so the rate stays meaningful. 6) CTE base: per-project actual_date = pj_a_5175_construction_complete_finish, planned_date = pj_p_5175_construction_complete_finish, gap_days = actual_date::date - planned_date::date (NULL when either is missing), delay_reason = pj_construction_complete_delay_code (surfaced as a GROUP BY dimension). 7) Aggregate per (rgn_region, m_area, m_market, construction_gc, delay_reason): total_sites = COUNT(DISTINCT pj_project_id); completed_count = COUNT(DISTINCT projects with actual_date NOT NULL); pending_count = COUNT(DISTINCT projects with actual_date NULL); completion_rate_pct = ROUND(100.0 * completed_count / NULLIF(total_sites, 0), 2); avg_gap_days = ROUND(AVG(gap_days)::numeric, 0); max_gap_days = MAX(gap_days); on_time_count = COUNT(DISTINCT projects with gap_days <= 0); delayed_count = COUNT(DISTINCT projects with gap_days > 0); on_time_pct = ROUND(100.0 * on_time_count / NULLIF(on_time_count + delayed_count, 0), 2).',
#     n.kpi_formula_description = 'Per (rgn_region, m_area, m_market, construction_gc, delay_reason): completion_rate_pct = ROUND(100.0 * COUNT(DISTINCT pj_project_id where pj_a_5175_construction_complete_finish IS NOT NULL) / NULLIF(COUNT(DISTINCT pj_project_id), 0), 2); gap_days = pj_a_5175_construction_complete_finish::date - pj_p_5175_construction_complete_finish::date; avg_gap_days = ROUND(AVG(gap_days)::numeric, 0); on_time_count = COUNT where gap_days <= 0; delayed_count = COUNT where gap_days > 0; on_time_pct = ROUND(100.0 * on_time_count / NULLIF(on_time_count + delayed_count, 0), 2).',
#     n.nl_description = 'Swap/Construction Completion Rate — measures, for each region / area / market / GC and delay reason, what share of AHLOB swaps or NTM constructions have been completed. Reports completed and pending counts, completion percentage, the average and maximum gap between planned and actual completion dates, and on-time vs delayed counts. Delay reasons surface why some completions slipped past plan. AHLOB and NTM share the same underlying milestone, so this single KPI covers both programs.',
#     n.definition = 'Swap/Construction Completion Rate — measures, for each region / area / market / GC and delay reason, what share of AHLOB swaps or NTM constructions have been completed. Reports completed and pending counts, completion percentage, the average and maximum gap between planned and actual completion dates, and on-time vs delayed counts. Delay reasons surface why some completions slipped past plan. AHLOB and NTM share the same underlying milestone, so this single KPI covers both programs.',
#     n.kpi_description = 'Swap/Construction Completion Rate — measures, for each region / area / market / GC and delay reason, what share of AHLOB swaps or NTM constructions have been completed. Reports completed and pending counts, completion percentage, the average and maximum gap between planned and actual completion dates, and on-time vs delayed counts. Delay reasons surface why some completions slipped past plan. AHLOB and NTM share the same underlying milestone, so this single KPI covers both programs.'
# RETURN n.label AS label, n.node_id AS node_id;