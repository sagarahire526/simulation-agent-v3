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

# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: '109ef604-2e52-4082-8ebe-d4297e9daa52'})
# SET
#     n.definition = 'SCOP Acceptance Cycle Time = business days (Mon-Fri, ISODOW 1-5) from ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl to ms_1559_cop_approved_by_t_mobile_actual, computed per project/site/package via LATERAL generate_series and aggregated by (rgn_region, m_area, m_market, construction_gc). 14-day SLA bucketing: within_sla_count (<= 14 days) vs without_sla_count (> 14 days) with within_sla_pct, plus avg/min/max business days. Only counts completed cycles (both timestamps NOT NULL, accepted >= submitted). Source: stg_ndpd_mbt_tmobile_macro_combined.',
#     n.kpi_business_logic = '1) Source: stg_ndpd_mbt_tmobile_macro_combined (single table, no JOIN). 2) Per-row cycle time computed by CROSS JOIN LATERAL generate_series between ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl::date and ms_1559_cop_approved_by_t_mobile_actual::date with 1-day step, then COUNT(*) over days where ISODOW BETWEEN 1 AND 5 (business days, Mon-Fri). 3) Mandatory predicates: both submission and acceptance timestamps NOT NULL; accepted >= submitted (guards against malformed data); ISODOW between 1 and 5 (only counts weekdays). 4) Apply optional equality filters: smp_name, smp_id, rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, package_name, por_category. 5) Apply optional date range (start_date / end_date on ms_1559_cop_approved_by_t_mobile_actual::date). 6) GROUP per-row outputs to geo grain (rgn_region, m_area, m_market, construction_gc). 7) Compute totals + SLA buckets: total_sites_accepted = COUNT(DISTINCT pj_project_id); within_sla_count = COUNT(DISTINCT) where business_day_diff <= 14; without_sla_count = COUNT(DISTINCT) where business_day_diff > 14; within_sla_pct = 100 * within_sla_count / NULLIF(total_sites_accepted, 0); avg/min/max_business_days aggregates over the per-site business_day_diff.',
#     n.kpi_contract = '{"function_name": "get_scop_acceptance_cycle_time", "node_type": "kpi", "node_id": "109ef604-2e52-4082-8ebe-d4297e9daa52", "node_label": "SCOP Acceptance Cycle Time", "description": "SCOP Acceptance Cycle Time \\u2014 measures how long the customer takes to accept the Tower-Work (TW) SCOP checklist after submission, in BUSINESS DAYS (Mon-Fri only, ISODOW 1-5). Computed per project/site/package via CROSS JOIN LATERAL generate_series between ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl::date and ms_1559_cop_approved_by_t_mobile_actual::date, then aggregated at (rgn_region, m_area, m_market, construction_gc) grain.\\n\\nPrimary outputs:\\n- total_sites_accepted : completed-cycle count (denominator)\\n- within_sla_count / without_sla_count : 14-day business-day SLA buckets\\n- within_sla_pct : SLA compliance percentage\\n- avg/min/max_business_days : cycle-time aggregates\\n\\nWhen to use this node: agent should pick this for any question about SCOP/COP acceptance speed, customer SLA compliance, days-to-acceptance, \'14-day SLA\', or cycle time between TW checklist submission and customer acceptance. Distinct from SCOP/COP Quality Rate (measures FTR rate / approval quality, not speed) and SCOP Approval Pending (measures pending volume awaiting TMO approval, not closed cycles).", "parameters": [{"name": "smp_name", "type": "string", "description": "Filter by program (e.g., \'NTM\', \'AHLOB Modernization\').", "required": false, "sample_values": []}, {"name": "smp_id", "type": "string", "description": "Filter by smp_id.", "required": false, "sample_values": []}, {"name": "rgn_region", "type": "string", "description": "Filter by region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Filter by area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Filter by market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Filter by GC.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Filter by pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Filter by s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Filter by customer_site_code.", "required": false, "sample_values": []}, {"name": "package_name", "type": "string", "description": "Filter by package_name.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Filter by por_category.", "required": false, "sample_values": []}, {"name": "start_date", "type": "date", "description": "Lower bound on acceptance date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on acceptance date.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": true}, {"name": "m_area", "type": "string", "nullable": true}, {"name": "m_market", "type": "string", "nullable": true}, {"name": "construction_gc", "type": "string", "nullable": true}, {"name": "total_sites_accepted", "type": "number", "nullable": false}, {"name": "within_sla_count", "type": "number", "nullable": false}, {"name": "without_sla_count", "type": "number", "nullable": false}, {"name": "within_sla_pct", "type": "number", "nullable": true}, {"name": "avg_business_days", "type": "number", "nullable": true}, {"name": "min_business_days", "type": "number", "nullable": true}, {"name": "max_business_days", "type": "number", "nullable": true}], "sample_output": [], "row_count": 10}',
#     n.kpi_filters = '[{"name": "smp_name", "type": "string", "description": "Optional equality filter on m.smp_name (program filter)."}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on m.smp_id."}, {"name": "rgn_region", "type": "string", "description": "Optional equality filter on m.rgn_region."}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m.m_area."}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m.m_market."}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on m.construction_gc."}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on m.pj_project_id."}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on m.s_site_id."}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on m.customer_site_code."}, {"name": "package_name", "type": "string", "description": "Optional equality filter on m.package_name."}, {"name": "por_category", "type": "string", "description": "Optional equality filter on m.por_category."}, {"name": "start_date", "type": "date", "description": "Lower bound on ms_1559_cop_approved_by_t_mobile_actual::date."}, {"name": "end_date", "type": "date", "description": "Upper bound on ms_1559_cop_approved_by_t_mobile_actual::date."}]',
#     n.kpi_formula_description = 'Business-day cycle time = COUNT(*) over generate_series(ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl::date, ms_1559_cop_approved_by_t_mobile_actual::date, INTERVAL \'1 day\') WHERE EXTRACT(ISODOW FROM g) BETWEEN 1 AND 5 (Mon-Fri only). within_sla_count = sites where business_day_diff <= 14; without_sla_count = sites where business_day_diff > 14; within_sla_pct = 100.0 * within_sla_count / NULLIF(total_sites_accepted, 0). Aggregated by (rgn_region, m_area, m_market, construction_gc) over completed cycles only (both submitted and accepted timestamps NOT NULL, accepted >= submitted).',
#     n.kpi_python_function = 'def get_scop_acceptance_cycle_time(execute_query, filters=None) -> list[dict]:\n    """SCOP Acceptance Cycle Time.\n\n    Measures how long the customer takes to accept the Tower-Work (TW) SCOP\n    checklist after submission, in BUSINESS DAYS (Mon-Fri only). Bucketed by\n    14-day SLA: within_sla (<= 14 business days) vs without_sla (> 14 business\n    days). Source: pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined\n    (single table, no JOIN).\n\n    Per-row business-day cycle:\n        COUNT(*) FROM generate_series(\n            ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl::date,\n            ms_1559_cop_approved_by_t_mobile_actual::date,\n            INTERVAL \'1 day\'\n        ) g\n        WHERE EXTRACT(ISODOW FROM g) BETWEEN 1 AND 5\n\n    Output (one row per (rgn_region, m_area, m_market, construction_gc)):\n      - total_sites_accepted : COUNT(DISTINCT pj_project_id) of completed cycles\n      - within_sla_count     : completed cycles where business_day_diff <= 14\n      - without_sla_count    : completed cycles where business_day_diff >  14\n      - within_sla_pct       : 100 * within_sla_count / total_sites_accepted\n      - avg_business_days, min_business_days, max_business_days\n\n    Optional filters (all equality, all optional):\n      - smp_name             : program filter (e.g., NTM, AHLOB Modernization)\n      - smp_id, pj_project_id, s_site_id, customer_site_code\n      - rgn_region, m_area, m_market, construction_gc\n      - package_name, por_category\n      - start_date, end_date : applied to ms_1559_cop_approved_by_t_mobile_actual::date\n    """\n    filters = filters or {}\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    where_parts = [\n        "m.\\"ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl\\" IS NOT NULL",\n        "m.\\"ms_1559_cop_approved_by_t_mobile_actual\\" IS NOT NULL",\n        "m.\\"ms_1559_cop_approved_by_t_mobile_actual\\"::date >= m.\\"ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl\\"::date",\n        "EXTRACT(ISODOW FROM g) BETWEEN 1 AND 5",\n    ]\n\n    # Date-range filters on the acceptance timestamp\n    if filters.get("start_date"):\n        where_parts.append(\n            f"m.\\"ms_1559_cop_approved_by_t_mobile_actual\\"::date >= DATE \'{_esc(filters[\'start_date\'])}\'"\n        )\n    if filters.get("end_date"):\n        where_parts.append(\n            f"m.\\"ms_1559_cop_approved_by_t_mobile_actual\\"::date <= DATE \'{_esc(filters[\'end_date\'])}\'"\n        )\n\n    # Open filter surface — optional equality filters\n    optional_eq_cols = [\n        "smp_name",\n        "smp_id",\n        "rgn_region",\n        "m_area",\n        "m_market",\n        "construction_gc",\n        "pj_project_id",\n        "s_site_id",\n        "customer_site_code",\n        "package_name",\n        "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            vv = _esc(v)\n            where_parts.append(f"m.\\"{col}\\" = \'{vv}\'")\n\n    where_sql = " AND ".join(where_parts)\n\n    sql = f"""\n        WITH cycle_times AS (\n            SELECT\n                m."rgn_region",\n                m."m_area",\n                m."m_market",\n                m."construction_gc",\n                m."pj_project_id",\n                m."s_site_id",\n                m."package_name",\n                COUNT(*) AS business_day_diff\n            FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined m\n            CROSS JOIN LATERAL generate_series(\n                m."ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl"::date,\n                m."ms_1559_cop_approved_by_t_mobile_actual"::date,\n                INTERVAL \'1 day\'\n            ) AS g\n            WHERE {where_sql}\n            GROUP BY\n                m."rgn_region",\n                m."m_area",\n                m."m_market",\n                m."construction_gc",\n                m."pj_project_id",\n                m."s_site_id",\n                m."package_name"\n        )\n        SELECT\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc",\n\n            COUNT(DISTINCT "pj_project_id") AS total_sites_accepted,\n\n            COUNT(DISTINCT CASE WHEN business_day_diff <= 14\n                                THEN "pj_project_id" END) AS within_sla_count,\n\n            COUNT(DISTINCT CASE WHEN business_day_diff >  14\n                                THEN "pj_project_id" END) AS without_sla_count,\n\n            (100.0 *\n                COUNT(DISTINCT CASE WHEN business_day_diff <= 14\n                                    THEN "pj_project_id" END)\n                / NULLIF(COUNT(DISTINCT "pj_project_id"), 0)\n            ) AS within_sla_pct,\n\n            AVG(business_day_diff)::float AS avg_business_days,\n            MIN(business_day_diff) AS min_business_days,\n            MAX(business_day_diff) AS max_business_days\n\n        FROM cycle_times\n        GROUP BY "rgn_region", "m_area", "m_market", "construction_gc"\n        ORDER BY "rgn_region", "m_area", "m_market", "construction_gc"\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_columns = ['stg_ndpd_mbt_tmobile_macro_combined.pj_project_id', 'stg_ndpd_mbt_tmobile_macro_combined.s_site_id', 'stg_ndpd_mbt_tmobile_macro_combined.customer_site_code', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc', 'stg_ndpd_mbt_tmobile_macro_combined.package_name', 'stg_ndpd_mbt_tmobile_macro_combined.por_category', 'stg_ndpd_mbt_tmobile_macro_combined.ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl', 'stg_ndpd_mbt_tmobile_macro_combined.ms_1559_cop_approved_by_t_mobile_actual'],
#     n.nl_description = 'SCOP Acceptance Cycle Time = business days (Mon-Fri, ISODOW 1-5) from ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl to ms_1559_cop_approved_by_t_mobile_actual, computed per project/site/package via LATERAL generate_series and aggregated by (rgn_region, m_area, m_market, construction_gc). 14-day SLA bucketing: within_sla_count (<= 14 days) vs without_sla_count (> 14 days) with within_sla_pct, plus avg/min/max business days. Only counts completed cycles (both timestamps NOT NULL, accepted >= submitted). Source: stg_ndpd_mbt_tmobile_macro_combined.'
# RETURN n.label AS label, n.node_id AS node_id;