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


# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'a1205491-3abb-41be-b638-cbef40e44441'})
# SET
#     n.kpi_python_function = 'def get_scop_cop_quality_rate(execute_query, filters=None) -> list[dict]:\n    """SCOP/COP Quality Rate (FTR%) — aligned with client-validated logic.\n\n    Source: stg_ndpd_mbt_tmobile_macro_combined (no JOIN).\n\n    Canonical predicates (client-validated):\n      - Accepted   : "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n      - FTR        : accepted AND "scop_tw_checklist_rejected_by_tmo" IS NULL\n      - Rejected   : "scop_tw_checklist_rejected_by_tmo" IS NOT NULL\n      - Resubmitted: "scop_tw_punch_list_re_submitted_to_tmo" IS NOT NULL\n      - Cycle days : ms_1559_cop_approved_by_t_mobile_actual::date\n                       - ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl::date\n                     (only when both NOT NULL and diff >= 0)\n\n    Hard guard filters (always applied — client validated):\n      - "rgn_region"      IS NOT NULL\n      - "construction_gc" IS NOT NULL\n      - "construction_gc" <> \'NOKIA\'\n\n    Output (one row per (rgn_region, m_area, m_market, construction_gc)):\n      - accepted_count, ftr_count, rejected_count, resubmission_count\n      - scop_ftr_rate_pct = ROUND(100 * ftr / NULLIF(accepted, 0), 2)\n      - avg_cycle_days    = ROUND(AVG(...), 0)\n      - max_cycle_days\n\n    Optional filters (all equality, all optional):\n      smp_name, smp_id,\n      rgn_region, m_area, m_market, construction_gc,\n      pj_project_id, s_site_id, customer_site_code,\n      package_name, por_category,\n      start_date, end_date (applied to ms_1559_cop_approved_by_t_mobile_actual::date).\n    """\n    filters = filters or {}\n\n    # Hard guard filters (client-validated, always present)\n    where_parts = [\n        \'"rgn_region" IS NOT NULL\',\n        \'"construction_gc" IS NOT NULL\',\n        \'"construction_gc" <> \\\'NOKIA\\\'\',\n    ]\n\n    optional_eq_cols = [\n        "smp_name",\n        "smp_id",\n        "rgn_region",\n        "m_area",\n        "m_market",\n        "construction_gc",\n        "pj_project_id",\n        "s_site_id",\n        "customer_site_code",\n        "package_name",\n        "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            vv = str(v).replace("\'", "\'\'")\n            where_parts.append(f"\\"{col}\\" = \'{vv}\'")\n\n    if filters.get("start_date"):\n        sd = str(filters["start_date"]).replace("\'", "\'\'")\n        where_parts.append(\n            f"\\"ms_1559_cop_approved_by_t_mobile_actual\\"::date >= DATE \'{sd}\'"\n        )\n    if filters.get("end_date"):\n        ed = str(filters["end_date"]).replace("\'", "\'\'")\n        where_parts.append(\n            f"\\"ms_1559_cop_approved_by_t_mobile_actual\\"::date <= DATE \'{ed}\'"\n        )\n\n    where_sql = "WHERE " + " AND ".join(where_parts)\n\n    sql = f"""\n        SELECT\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc",\n\n            COUNT(DISTINCT CASE\n                WHEN "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n                THEN "pj_project_id"\n            END) AS accepted_count,\n\n            COUNT(DISTINCT CASE\n                WHEN "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n                 AND "scop_tw_checklist_rejected_by_tmo" IS NULL\n                THEN "pj_project_id"\n            END) AS ftr_count,\n\n            COUNT(DISTINCT CASE\n                WHEN "scop_tw_checklist_rejected_by_tmo" IS NOT NULL\n                THEN "pj_project_id"\n            END) AS rejected_count,\n\n            COUNT(DISTINCT CASE\n                WHEN "scop_tw_punch_list_re_submitted_to_tmo" IS NOT NULL\n                THEN "pj_project_id"\n            END) AS resubmission_count,\n\n            ROUND(\n                (100.0 *\n                    COUNT(DISTINCT CASE\n                        WHEN "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n                         AND "scop_tw_checklist_rejected_by_tmo" IS NULL\n                        THEN "pj_project_id"\n                    END)\n                    / NULLIF(\n                        COUNT(DISTINCT CASE\n                            WHEN "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n                            THEN "pj_project_id"\n                        END),\n                        0\n                    )\n                ),\n                2\n            ) AS scop_ftr_rate_pct,\n\n            ROUND(\n                AVG(CASE\n                    WHEN "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n                     AND "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl" IS NOT NULL\n                     AND ("ms_1559_cop_approved_by_t_mobile_actual"::date\n                          - "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl"::date) >= 0\n                    THEN ("ms_1559_cop_approved_by_t_mobile_actual"::date\n                          - "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl"::date)\n                END),\n                0\n            ) AS avg_cycle_days,\n\n            MAX(CASE\n                WHEN "ms_1559_cop_approved_by_t_mobile_actual" IS NOT NULL\n                 AND "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl" IS NOT NULL\n                 AND ("ms_1559_cop_approved_by_t_mobile_actual"::date\n                      - "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl"::date) >= 0\n                THEN ("ms_1559_cop_approved_by_t_mobile_actual"::date\n                      - "ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl"::date)\n            END) AS max_cycle_days\n\n        FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined\n        {where_sql}\n        GROUP BY\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc"\n        ORDER BY\n            "rgn_region",\n            "m_area",\n            "m_market",\n            "construction_gc"\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_business_logic = '1) Single source: pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined — NO JOIN, NO smp_name branching. Same formula applies to both NTM and AHLOB Modernization; smp_name is one of the optional equality filters the agent can pass. 2) Hard guard filters (always applied, client-validated): "rgn_region" IS NOT NULL; "construction_gc" IS NOT NULL; "construction_gc" <> \'NOKIA\'. 3) Apply optional equality filters (open filter surface — pass any subset): smp_name, smp_id, rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, package_name, por_category. 4) Apply optional date range (start_date / end_date) on ms_1559_cop_approved_by_t_mobile_actual::date. 5) Group by (rgn_region, m_area, m_market, construction_gc). 6) accepted_count = COUNT(DISTINCT pj_project_id) WHERE ms_1559_cop_approved_by_t_mobile_actual IS NOT NULL. 7) ftr_count = COUNT(DISTINCT pj_project_id) WHERE accepted AND scop_tw_checklist_rejected_by_tmo IS NULL  (client-validated; replaces the old cop_approval_type IS NULL signal). 8) rejected_count = COUNT(DISTINCT pj_project_id) WHERE scop_tw_checklist_rejected_by_tmo IS NOT NULL. 9) resubmission_count = COUNT(DISTINCT pj_project_id) WHERE scop_tw_punch_list_re_submitted_to_tmo IS NOT NULL. 10) scop_ftr_rate_pct = ROUND(100.0 * ftr_count / NULLIF(accepted_count, 0), 2). 11) avg_cycle_days / max_cycle_days = aggregates of (ms_1559_cop_approved_by_t_mobile_actual::date - ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl::date) when both timestamps are NOT NULL and the diff is >= 0; avg is ROUND(..., 0).',
#     n.kpi_formula_description = 'scop_ftr_rate_pct = ROUND(100.0 * ftr_count / NULLIF(accepted_count, 0), 2), where accepted_count = COUNT(DISTINCT pj_project_id) with ms_1559_cop_approved_by_t_mobile_actual IS NOT NULL, and ftr_count = COUNT(DISTINCT pj_project_id) with that condition AND scop_tw_checklist_rejected_by_tmo IS NULL (client-validated). Hard guards: rgn_region IS NOT NULL, construction_gc IS NOT NULL, construction_gc <> \'NOKIA\'. Single unified formula — same logic for both NTM and AHLOB Modernization; smp_name is just an optional filter. Output also includes raw counts (accepted_count, ftr_count, rejected_count, resubmission_count) and acceptance cycle days (avg_cycle_days = ROUND(AVG(...), 0), max_cycle_days = MAX(...)) when both ms_1559_cop_approved_by_t_mobile_actual and ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl are NOT NULL and diff >= 0. Grouped by (rgn_region, m_area, m_market, construction_gc).',
#     n.nl_description = 'SCOP/COP Quality Rate (FTR%) = ROUND(100 * (distinct accepted projects with no TMO rejection on the checklist) / (distinct accepted projects), 2), grouped by (rgn_region, m_area, m_market, construction_gc). Source: stg_ndpd_mbt_tmobile_macro_combined. Single unified formula for both NTM and AHLOB Modernization; smp_name is just an optional filter. Acceptance = ms_1559_cop_approved_by_t_mobile_actual IS NOT NULL; FTR additionally requires scop_tw_checklist_rejected_by_tmo IS NULL (client-validated). Hard guards always applied: rgn_region IS NOT NULL, construction_gc IS NOT NULL, construction_gc <> \'NOKIA\'. Includes raw counts (accepted_count, ftr_count, rejected_count, resubmission_count) and acceptance cycle days (avg/max) from ms_1559::date - ms_1557::date. COP and SCOP are synonymous; distinct from SCOP Approval Pending (which measures pending volume, not quality).',
#     n.definition = 'SCOP/COP Quality Rate (FTR%) = ROUND(100 * (distinct accepted projects with no TMO rejection on the checklist) / (distinct accepted projects), 2), grouped by (rgn_region, m_area, m_market, construction_gc). Source: stg_ndpd_mbt_tmobile_macro_combined. Single unified formula for both NTM and AHLOB Modernization; smp_name is just an optional filter. Acceptance = ms_1559_cop_approved_by_t_mobile_actual IS NOT NULL; FTR additionally requires scop_tw_checklist_rejected_by_tmo IS NULL (client-validated). Hard guards always applied: rgn_region IS NOT NULL, construction_gc IS NOT NULL, construction_gc <> \'NOKIA\'. Includes raw counts (accepted_count, ftr_count, rejected_count, resubmission_count) and acceptance cycle days (avg/max) from ms_1559::date - ms_1557::date. COP and SCOP are synonymous; distinct from SCOP Approval Pending (which measures pending volume, not quality).',
#     n.kpi_description = 'SCOP/COP Quality Rate (FTR%) = ROUND(100 * (distinct accepted projects with no TMO rejection on the checklist) / (distinct accepted projects), 2), grouped by (rgn_region, m_area, m_market, construction_gc). Source: stg_ndpd_mbt_tmobile_macro_combined. Single unified formula for both NTM and AHLOB Modernization; smp_name is just an optional filter. Acceptance = ms_1559_cop_approved_by_t_mobile_actual IS NOT NULL; FTR additionally requires scop_tw_checklist_rejected_by_tmo IS NULL (client-validated). Hard guards always applied: rgn_region IS NOT NULL, construction_gc IS NOT NULL, construction_gc <> \'NOKIA\'. Includes raw counts (accepted_count, ftr_count, rejected_count, resubmission_count) and acceptance cycle days (avg/max) from ms_1559::date - ms_1557::date. COP and SCOP are synonymous; distinct from SCOP Approval Pending (which measures pending volume, not quality).',
#     n.kpi_source_columns = ['stg_ndpd_mbt_tmobile_macro_combined.pj_project_id', 'stg_ndpd_mbt_tmobile_macro_combined.s_site_id', 'stg_ndpd_mbt_tmobile_macro_combined.customer_site_code', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc', 'stg_ndpd_mbt_tmobile_macro_combined.package_name', 'stg_ndpd_mbt_tmobile_macro_combined.por_category', 'stg_ndpd_mbt_tmobile_macro_combined.scop_tw_checklist_submitted_to_customer', 'stg_ndpd_mbt_tmobile_macro_combined.scop_checklist_accepted_by_customer', 'stg_ndpd_mbt_tmobile_macro_combined.scop_tw_checklist_rejected_by_tmo', 'stg_ndpd_mbt_tmobile_macro_combined.scop_tw_punch_list_re_submitted_to_tmo', 'stg_ndpd_mbt_tmobile_macro_combined.ms_1557_punch_checklist_reviewed_and_submitted_to_tmobile_atl', 'stg_ndpd_mbt_tmobile_macro_combined.ms_1559_cop_approved_by_t_mobile_actual']
# RETURN n.label AS label, n.node_id AS node_id;