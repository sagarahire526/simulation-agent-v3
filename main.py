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



# MATCH (n:BKGNode { session_id: '69a3d22f26e208edc083a06e', node_id: '313756f8-da42-470a-998f-dc558b1940f3' })
# SET
#     n.label = 'H&S/HSE Compliance Rate',
#     n.kpi_name = 'H&S/HSE Compliance Rate',
#     n.kpi_formula_description = 'compliance_rate_pct = 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0), where attempted_reviews = COUNT(DISTINCT h.site_id) with h.gsd_hse_review_check_date IS NOT NULL AND h.check_in_status IS NOT NULL after INNER JOIN of stg_ndpd_hse_site_checklist (h) to stg_ndpd_mbt_tmobile_macro_combined (m) on smp_id, and compliant_reviews = COUNT(DISTINCT h.site_id) within the attempted set whose h.non_compliance_status = \'Ok\'. Grouped by m.rgn_region, m.m_area, m.m_market, m.construction_gc.',
#     n.kpi_output_schema = '[{"column": "rgn_region", "type": "string", "description": "Region from macro_combined."}, {"column": "m_area", "type": "string", "description": "Area from macro_combined."}, {"column": "m_market", "type": "string", "description": "Market from macro_combined."}, {"column": "construction_gc", "type": "string", "description": "General contractor from macro_combined."}, {"column": "attempted_reviews", "type": "int", "description": "Distinct sites with a review attempt (gsd_hse_review_check_date IS NOT NULL AND check_in_status IS NOT NULL)."}, {"column": "compliant_reviews", "type": "int", "description": "Distinct attempted sites whose non_compliance_status = \'Ok\' (overall H&S/HSE pass)."}, {"column": "compliance_rate_pct", "type": "float", "description": "Percent of attempted reviews where non_compliance_status = \'Ok\'."}, {"column": "non_compliant_count", "type": "int", "description": "Distinct attempted sites with non_compliance_status = \'Not Ok\'."}, {"column": "fail_check_in_count", "type": "int", "description": "Distinct attempted sites with check_in_status = \'Not Done\'."}, {"column": "fail_ppe_count", "type": "int", "description": "Distinct attempted sites with ppe_validation IN (\'Fail\',\'Failed\')."}, {"column": "fail_jsa_count", "type": "int", "description": "Distinct attempted sites with jsa_completion_status = \'Not Completed\'."}, {"column": "fail_ptid_l2w_count", "type": "int", "description": "Distinct attempted sites with each_crew_ptid_l2w_status = \'Not Available\'."}]',
#     n.kpi_business_logic = '1) INNER JOIN stg_ndpd_hse_site_checklist (h) to stg_ndpd_mbt_tmobile_macro_combined (m) on h.smp_id = m.smp_id. 2) Keep rows with h.gsd_hse_review_check_date IS NOT NULL AND h.check_in_status IS NOT NULL (when check_in_status is NULL the rest of the checklist record is NULL too). 3) Apply optional macro-side filters (m.rgn_region, m.m_area, m.m_market, m.construction_gc, m.smp_id, m.smp_name) and optional checklist-side filters (h.site_id, h.customer_name, h.hse_status, h.non_compliance_status, h.non_complaince_category, h.hse_engineer_name) plus optional date range (start_date / end_date on h.gsd_hse_review_check_date). 4) Group by m.rgn_region, m.m_area, m.m_market, m.construction_gc. 5) attempted_reviews = COUNT(DISTINCT h.site_id) per group. 6) compliant_reviews = COUNT(DISTINCT h.site_id) where h.non_compliance_status = \'Ok\'. 7) compliance_rate_pct = 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0). 8) Diagnostic gate counts using actual column values: non_compliant_count = COUNT(DISTINCT h.site_id) where h.non_compliance_status = \'Not Ok\'; fail_check_in_count where h.check_in_status = \'Not Done\'; fail_ppe_count where h.ppe_validation IN (\'Fail\',\'Failed\'); fail_jsa_count where h.jsa_completion_status = \'Not Completed\'; fail_ptid_l2w_count where h.each_crew_ptid_l2w_status = \'Not Available\'. 9) Use DISTINCT h.site_id to avoid double-counting multiple checklist rows per site.',
#     n.kpi_filters = '[{"name": "start_date", "type": "date", "description": "Filters to rows with h.gsd_hse_review_check_date >= start_date."}, {"name": "end_date", "type": "date", "description": "Filters to rows with h.gsd_hse_review_check_date < (end_date + 1 day)."}, {"name": "rgn_region", "type": "string", "description": "Filters by m.rgn_region."}, {"name": "m_area", "type": "string", "description": "Filters by m.m_area."}, {"name": "m_market", "type": "string", "description": "Filters by m.m_market."}, {"name": "construction_gc", "type": "string", "description": "Filters by m.construction_gc."}, {"name": "smp_id", "type": "string", "description": "Filters by m.smp_id."}, {"name": "smp_name", "type": "string", "description": "Filters by m.smp_name (e.g., \'NTM\', \'AHLOB Modernization\')."}, {"name": "site_id", "type": "string", "description": "Filters by h.site_id."}, {"name": "customer_name", "type": "string", "description": "Filters by h.customer_name."}, {"name": "hse_status", "type": "string", "description": "Filters by h.hse_status."}, {"name": "non_compliance_status", "type": "string", "description": "Filters by h.non_compliance_status (\'Ok\' or \'Not Ok\')."}, {"name": "non_complaince_category", "type": "string", "description": "Filters by h.non_complaince_category (note: column name preserves source spelling)."}, {"name": "hse_engineer_name", "type": "string", "description": "Filters by h.hse_engineer_name."}]',
#     n.kpi_contract = '{"function_name": "get_hse_compliance_rate", "node_type": "kpi", "node_id": "313756f8-da42-470a-998f-dc558b1940f3", "node_label": "H&S/HSE Compliance Rate", "description": "Percent of HSE site reviews that pass H&S/HSE compliance, rolled up by macro project geo dimensions (rgn_region, m_area, m_market, construction_gc). Source: stg_ndpd_hse_site_checklist (h) INNER JOINed to stg_ndpd_mbt_tmobile_macro_combined (m) on smp_id.\\n\\nDenominator (attempted_reviews): COUNT(DISTINCT h.site_id) WHERE h.gsd_hse_review_check_date IS NOT NULL AND h.check_in_status IS NOT NULL. These two filters are mandatory because when check_in_status is NULL the rest of the checklist record is NULL too (no real review attempt for that site).\\n\\nNumerator (compliant_reviews): COUNT(DISTINCT h.site_id) within the attempted set where h.non_compliance_status = \'Ok\'. The single column non_compliance_status is the official pass/fail signal (Ok = passed, Not Ok = failed).\\n\\nRate: 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0).\\n\\nDiagnostic gate counts (use actual column values):\\n- non_compliant_count: h.non_compliance_status = \'Not Ok\'\\n- fail_check_in_count: h.check_in_status = \'Not Done\'\\n- fail_ppe_count: h.ppe_validation IN (\'Fail\',\'Failed\')\\n- fail_jsa_count: h.jsa_completion_status = \'Not Completed\'\\n- fail_ptid_l2w_count: h.each_crew_ptid_l2w_status = \'Not Available\'\\n\\nDISTINCT h.site_id avoids double-counting multiple checklist rows per site. H&S and HSE are synonyms \\u2014 both refer to this same KPI.", "parameters": [{"name": "start_date", "type": "date", "description": "Filter by start date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Filter by end date.", "required": false, "sample_values": []}, {"name": "rgn_region", "type": "string", "description": "Filter by m.rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Filter by m.m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Filter by m.m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Filter by m.construction_gc.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "Program filter on m.smp_name (e.g., \'NTM\', \'AHLOB Modernization\').", "required": false, "sample_values": ["NTM", "AHLOB Modernization"]}, {"name": "non_compliance_status", "type": "string", "description": "Filter by h.non_compliance_status (\'Ok\' or \'Not Ok\').", "required": false, "sample_values": ["Ok", "Not Ok"]}, {"name": "non_complaince_category", "type": "string", "description": "Filter by h.non_complaince_category.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": true}, {"name": "m_area", "type": "string", "nullable": true}, {"name": "m_market", "type": "string", "nullable": true}, {"name": "construction_gc", "type": "string", "nullable": true}, {"name": "attempted_reviews", "type": "number", "nullable": false}, {"name": "compliant_reviews", "type": "number", "nullable": false}, {"name": "compliance_rate_pct", "type": "number", "nullable": true}, {"name": "non_compliant_count", "type": "number", "nullable": false}, {"name": "fail_check_in_count", "type": "number", "nullable": false}, {"name": "fail_ppe_count", "type": "number", "nullable": false}, {"name": "fail_jsa_count", "type": "number", "nullable": false}, {"name": "fail_ptid_l2w_count", "type": "number", "nullable": false}], "sample_output": [], "row_count": 10}',
#     n.nl_description = 'H&S/HSE Compliance Rate = 100 * (distinct sites with non_compliance_status = \'Ok\') / (distinct sites with a non-null review date and non-null check-in status), grouped by (rgn_region, m_area, m_market, construction_gc) — geo dimensions sourced from stg_ndpd_mbt_tmobile_macro_combined via INNER JOIN on smp_id. Includes diagnostic per-gate fail counts using actual column values. H&S and HSE are synonyms — both refer to this same KPI.',
#     n.definition = 'H&S/HSE Compliance Rate = 100 * (distinct sites with non_compliance_status = \'Ok\') / (distinct sites with a non-null review date and non-null check-in status), grouped by (rgn_region, m_area, m_market, construction_gc) — geo dimensions sourced from stg_ndpd_mbt_tmobile_macro_combined via INNER JOIN on smp_id. Includes diagnostic per-gate fail counts using actual column values. H&S and HSE are synonyms — both refer to this same KPI.',
#     n.kpi_python_function = 'def get_hse_compliance_rate(execute_query, filters=None) -> list[dict]:\n    """H&S/HSE Compliance Rate.\n\n    Grain: one row per (rgn_region, m_area, m_market, construction_gc).\n\n    A site passes H&S/HSE compliance when non_compliance_status = \'Ok\'.\n    An attempted review requires gsd_hse_review_check_date IS NOT NULL AND\n    check_in_status IS NOT NULL (when check_in_status is NULL, the rest of the\n    checklist record is NULL too, so the site has no valid review for that day).\n\n    Geo dimensions come from stg_ndpd_mbt_tmobile_macro_combined (m), joined to\n    stg_ndpd_hse_site_checklist (h) on smp_id.\n\n    Column value reference (h):\n      - check_in_status:             \'Done\' | \'Not Done\' | NULL (NULL excluded)\n      - ppe_validation:              \'Pass\' | \'Fail\' | \'Failed\'\n      - jsa_completion_status:       \'Completed\' | \'Not Completed\'\n      - each_crew_ptid_l2w_status:   \'Available\' | \'Not Available\'\n      - non_compliance_status:       \'Ok\' | \'Not Ok\'\n\n    Output columns:\n      - attempted_reviews:    distinct sites with a real review attempt\n      - compliant_reviews:    distinct sites with non_compliance_status = \'Ok\'\n      - compliance_rate_pct:  100 * compliant_reviews / attempted_reviews\n      - non_compliant_count:  distinct sites with non_compliance_status = \'Not Ok\'\n      - fail_check_in_count:  check_in_status   = \'Not Done\'\n      - fail_ppe_count:       ppe_validation    IN (\'Fail\',\'Failed\')\n      - fail_jsa_count:       jsa_completion_status = \'Not Completed\'\n      - fail_ptid_l2w_count:  each_crew_ptid_l2w_status = \'Not Available\'\n    """\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    filters = filters or {}\n    where_parts = [\n        "h.\\"gsd_hse_review_check_date\\" IS NOT NULL",\n        "h.\\"check_in_status\\" IS NOT NULL",\n    ]\n\n    # Date-range filters on the checklist date\n    if filters.get("start_date"):\n        where_parts.append(f"h.\\"gsd_hse_review_check_date\\" >= \'{_esc(filters[\'start_date\'])}\'")\n    if filters.get("end_date"):\n        where_parts.append(\n            f"h.\\"gsd_hse_review_check_date\\" < (\'{_esc(filters[\'end_date\'])}\'::date + interval \'1 day\')"\n        )\n\n    # Macro-side filters (geo + program)\n    for k in ["rgn_region", "m_area", "m_market", "construction_gc", "smp_id", "smp_name"]:\n        v = filters.get(k)\n        if v is not None and v != "":\n            where_parts.append(f"m.\\"{k}\\" = \'{_esc(v)}\'")\n\n    # Checklist-side filters\n    for k in [\n        "site_id",\n        "customer_name",\n        "hse_status",\n        "non_compliance_status",\n        "non_complaince_category",\n        "hse_engineer_name",\n    ]:\n        v = filters.get(k)\n        if v is not None and v != "":\n            where_parts.append(f"h.\\"{k}\\" = \'{_esc(v)}\'")\n\n    where_sql = "WHERE " + " AND ".join(where_parts)\n\n    sql = f"""\n        SELECT\n            m."rgn_region",\n            m."m_area",\n            m."m_market",\n            m."construction_gc",\n\n            COUNT(DISTINCT h."site_id") AS attempted_reviews,\n\n            COUNT(DISTINCT CASE\n                WHEN h."non_compliance_status" = \'Ok\' THEN h."site_id"\n            END) AS compliant_reviews,\n\n            (100.0 *\n                COUNT(DISTINCT CASE WHEN h."non_compliance_status" = \'Ok\' THEN h."site_id" END)\n                / NULLIF(COUNT(DISTINCT h."site_id"), 0)\n            ) AS compliance_rate_pct,\n\n            COUNT(DISTINCT CASE\n                WHEN h."non_compliance_status" = \'Not Ok\' THEN h."site_id"\n            END) AS non_compliant_count,\n\n            COUNT(DISTINCT CASE\n                WHEN h."check_in_status" = \'Not Done\' THEN h."site_id"\n            END) AS fail_check_in_count,\n\n            COUNT(DISTINCT CASE\n                WHEN h."ppe_validation" IN (\'Fail\',\'Failed\') THEN h."site_id"\n            END) AS fail_ppe_count,\n\n            COUNT(DISTINCT CASE\n                WHEN h."jsa_completion_status" = \'Not Completed\' THEN h."site_id"\n            END) AS fail_jsa_count,\n\n            COUNT(DISTINCT CASE\n                WHEN h."each_crew_ptid_l2w_status" = \'Not Available\' THEN h."site_id"\n            END) AS fail_ptid_l2w_count\n\n        FROM "public"."stg_ndpd_hse_site_checklist" h\n        INNER JOIN "public"."stg_ndpd_mbt_tmobile_macro_combined" m\n            ON h."smp_id" = m."smp_id"\n        {where_sql}\n        GROUP BY\n            m."rgn_region",\n            m."m_area",\n            m."m_market",\n            m."construction_gc"\n        ORDER BY\n            m."rgn_region",\n            m."m_area",\n            m."m_market",\n            m."construction_gc"\n    """\n\n    return execute_query(sql, db="public")',
#     n.kpi_description = 'Percent of HSE site reviews that pass H&S/HSE compliance, rolled up by macro project geo dims (rgn_region, m_area, m_market, construction_gc). A site passes when non_compliance_status = \'Ok\'; an attempted review requires gsd_hse_review_check_date IS NOT NULL AND check_in_status IS NOT NULL. Sourced from stg_ndpd_hse_site_checklist INNER JOINed to stg_ndpd_mbt_tmobile_macro_combined on smp_id (geo dimensions come from macro_combined). Includes per-gate diagnostic fail counts (Not Done check-in, Fail/Failed PPE, Not Completed JSA, Not Available PTID L2W). Optionally filterable by smp_name, region, area, market, GC, and other dimensions. H&S and HSE are synonyms.',
#     n.kpi_source_tables = ['public.stg_ndpd_hse_site_checklist', 'public.stg_ndpd_mbt_tmobile_macro_combined'],
#     n.kpi_source_columns = ['stg_ndpd_hse_site_checklist.smp_id', 'stg_ndpd_hse_site_checklist.site_id', 'stg_ndpd_hse_site_checklist.gsd_hse_review_check_date', 'stg_ndpd_hse_site_checklist.check_in_status', 'stg_ndpd_hse_site_checklist.ppe_validation', 'stg_ndpd_hse_site_checklist.jsa_completion_status', 'stg_ndpd_hse_site_checklist.each_crew_ptid_l2w_status', 'stg_ndpd_hse_site_checklist.non_compliance_status', 'stg_ndpd_hse_site_checklist.non_complaince_category', 'stg_ndpd_hse_site_checklist.customer_name', 'stg_ndpd_hse_site_checklist.hse_status', 'stg_ndpd_hse_site_checklist.hse_engineer_name', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc'],
#     n.kpi_dimensions = ['rgn_region', 'm_area', 'm_market', 'construction_gc']
# RETURN n.label AS label, n.node_id AS node_id, n.kpi_name AS kpi_name;