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
#     n.kpi_python_function = 'def get_hse_compliance_rate(execute_query, filters=None) -> list[dict]:\n    """H&S/HSE Compliance Rate.\n\n    Grain: one row per (rgn_region, m_area, m_market, construction_gc).\n\n    Two-CTE structure (matches client pattern):\n      - `hse_table`: pulls checklist rows with all row-level filters applied\n        (3 mandatory NOT NULL filters + optional date range, smp_name,\n        customer_name, hse_status, non_compliance_status,\n        non_complaince_category, hse_engineer_name). No DISTINCT here -\n        each surviving review row is one count unit.\n      - `combined_table`: `SELECT DISTINCT smp_id, rgn_region, m_area,\n        m_market, construction_gc` from macro_combined. NO WHERE - the\n        client\'s pattern keeps this side clean.\n\n    Then INNER JOIN on smp_id and aggregate. Optional geo filters\n    (rgn_region, m_area, m_market, construction_gc, smp_id) are applied in\n    the outer WHERE so combined_table stays unfiltered.\n\n    Counting (matches client - no DISTINCT, case-insensitive match on the\n    pass/fail signal columns):\n      - attempted_reviews   = COUNT(a.smp_id)\n      - compliant_reviews   = COUNT(CASE WHEN LOWER(non_compliance_status)=\'ok\'      THEN smp_id END)\n      - non_compliant_count = COUNT(CASE WHEN LOWER(non_compliance_status)=\'not ok\'  THEN smp_id END)\n      - compliance_rate_pct = 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0)\n\n    Diagnostic per-gate fail counts (also non-distinct, LOWER-matched):\n      - fail_check_in_count  : check_in_status            = \'not done\'\n      - fail_ppe_count       : ppe_validation             IN (\'fail\',\'failed\')\n      - fail_jsa_count       : jsa_completion_status      = \'not completed\'\n      - fail_ptid_l2w_count  : each_crew_ptid_l2w_status  = \'not available\'\n    """\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    filters = filters or {}\n\n    # ---- hse_table filters (checklist side; client\'s pattern) ----\n    hse_where = [\n        "\\"gsd_hse_review_check_date\\" IS NOT NULL",\n        "\\"check_in_status\\" IS NOT NULL",\n        "\\"non_compliance_status\\" IS NOT NULL",\n    ]\n    if filters.get("start_date"):\n        hse_where.append(\n            f"\\"gsd_hse_review_check_date\\"::date >= DATE \'{_esc(filters[\'start_date\'])}\'"\n        )\n    if filters.get("end_date"):\n        hse_where.append(\n            f"\\"gsd_hse_review_check_date\\"::date < DATE \'{_esc(filters[\'end_date\'])}\'"\n        )\n    if filters.get("smp_name"):\n        hse_where.append(f"\\"smp_name\\" = \'{_esc(filters[\'smp_name\'])}\'")\n\n    for k in [\n        "customer_name",\n        "hse_status",\n        "non_compliance_status",\n        "non_complaince_category",\n        "hse_engineer_name",\n    ]:\n        v = filters.get(k)\n        if v is not None and v != "":\n            hse_where.append(f"\\"{k}\\" = \'{_esc(v)}\'")\n\n    hse_where_sql = "WHERE " + " AND ".join(hse_where)\n\n    # ---- Outer (post-JOIN) geo filters; combined_table itself stays unfiltered ----\n    outer_where = []\n    for k in ["rgn_region", "m_area", "m_market", "construction_gc"]:\n        v = filters.get(k)\n        if v is not None and v != "":\n            outer_where.append(f"b.\\"{k}\\" = \'{_esc(v)}\'")\n    if filters.get("smp_id"):\n        outer_where.append(f"b.\\"smp_id\\" = \'{_esc(filters[\'smp_id\'])}\'")\n    outer_where_sql = ("WHERE " + " AND ".join(outer_where)) if outer_where else ""\n\n    sql = f"""\n        WITH hse_table AS (\n            SELECT\n                "smp_id",\n                "non_compliance_status",\n                "check_in_status",\n                "ppe_validation",\n                "jsa_completion_status",\n                "each_crew_ptid_l2w_status"\n            FROM "pwc_macro_staging_schema"."stg_ndpd_hse_site_checklist"\n            {hse_where_sql}\n        ),\n        combined_table AS (\n            SELECT DISTINCT\n                "smp_id",\n                "rgn_region",\n                "m_area",\n                "m_market",\n                "construction_gc"\n            FROM "pwc_macro_staging_schema"."stg_ndpd_mbt_tmobile_macro_combined"\n        )\n        SELECT\n            b."rgn_region",\n            b."m_area",\n            b."m_market",\n            b."construction_gc",\n\n            COUNT(a."smp_id") AS attempted_reviews,\n\n            COUNT(CASE WHEN LOWER(a."non_compliance_status") = \'ok\'\n                       THEN a."smp_id" END) AS compliant_reviews,\n\n            (100.0 *\n                COUNT(CASE WHEN LOWER(a."non_compliance_status") = \'ok\'\n                           THEN a."smp_id" END)\n                / NULLIF(COUNT(a."smp_id"), 0)\n            ) AS compliance_rate_pct,\n\n            COUNT(CASE WHEN LOWER(a."non_compliance_status") = \'not ok\'\n                       THEN a."smp_id" END) AS non_compliant_count,\n\n            COUNT(CASE WHEN LOWER(a."check_in_status") = \'not done\'\n                       THEN a."smp_id" END) AS fail_check_in_count,\n\n            COUNT(CASE WHEN LOWER(a."ppe_validation") IN (\'fail\',\'failed\')\n                       THEN a."smp_id" END) AS fail_ppe_count,\n\n            COUNT(CASE WHEN LOWER(a."jsa_completion_status") = \'not completed\'\n                       THEN a."smp_id" END) AS fail_jsa_count,\n\n            COUNT(CASE WHEN LOWER(a."each_crew_ptid_l2w_status") = \'not available\'\n                       THEN a."smp_id" END) AS fail_ptid_l2w_count\n\n        FROM hse_table a\n        JOIN combined_table b\n            ON a."smp_id" = b."smp_id"\n        {outer_where_sql}\n        GROUP BY\n            b."rgn_region",\n            b."m_area",\n            b."m_market",\n            b."construction_gc"\n        ORDER BY\n            b."rgn_region",\n            b."m_area",\n            b."m_market",\n            b."construction_gc"\n    """\n\n    return execute_query(sql, db="public")',
#     n.kpi_formula_description = 'compliance_rate_pct = 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0), where attempted_reviews = COUNT(a.smp_id) (NON-distinct, matches client) over the hse_table CTE (checklist rows where gsd_hse_review_check_date IS NOT NULL AND check_in_status IS NOT NULL AND non_compliance_status IS NOT NULL plus optional smp_name and other checklist-side filters). compliant_reviews = COUNT(CASE WHEN LOWER(a.non_compliance_status) = \'ok\' THEN a.smp_id END). Joined to combined_table (SELECT DISTINCT smp_id, rgn_region, m_area, m_market, construction_gc FROM macro_combined - no WHERE) on smp_id. Optional geo filters are applied in the outer WHERE so combined_table stays unfiltered. Grouped by rgn_region, m_area, m_market, construction_gc.',
#     n.kpi_business_logic = 'Two-CTE pattern (matches client query). 1) hse_table CTE: SELECT smp_id, non_compliance_status, check_in_status, ppe_validation, jsa_completion_status, each_crew_ptid_l2w_status FROM stg_ndpd_hse_site_checklist with WHERE gsd_hse_review_check_date IS NOT NULL AND check_in_status IS NOT NULL AND non_compliance_status IS NOT NULL, plus optional date range (gsd_hse_review_check_date::date between start_date and end_date), optional smp_name (applied to checklist\'s smp_name column, not macro), and optional checklist-side filters (customer_name, hse_status, non_compliance_status, non_complaince_category, hse_engineer_name). 2) combined_table CTE: SELECT DISTINCT smp_id, rgn_region, m_area, m_market, construction_gc FROM stg_ndpd_mbt_tmobile_macro_combined - NO WHERE clause; this side stays clean. 3) Outer query: hse_table a JOIN combined_table b ON a.smp_id = b.smp_id, optionally constrained by post-JOIN geo filters (b.rgn_region, b.m_area, b.m_market, b.construction_gc, b.smp_id) supplied via filters dict. 4) GROUP BY b.rgn_region, b.m_area, b.m_market, b.construction_gc. 5) attempted_reviews = COUNT(a.smp_id) (NON-distinct - one count per surviving review row). 6) compliant_reviews = COUNT(CASE WHEN LOWER(a.non_compliance_status) = \'ok\' THEN a.smp_id END). 7) compliance_rate_pct = 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0). 8) Diagnostic gate counts (each non-distinct, LOWER-matched): non_compliant_count where LOWER(non_compliance_status) = \'not ok\'; fail_check_in_count where LOWER(check_in_status) = \'not done\'; fail_ppe_count where LOWER(ppe_validation) IN (\'fail\',\'failed\'); fail_jsa_count where LOWER(jsa_completion_status) = \'not completed\'; fail_ptid_l2w_count where LOWER(each_crew_ptid_l2w_status) = \'not available\'.',
#     n.kpi_output_schema = '[{"column": "rgn_region", "type": "string", "description": "Region from macro_combined."}, {"column": "m_area", "type": "string", "description": "Area from macro_combined."}, {"column": "m_market", "type": "string", "description": "Market from macro_combined."}, {"column": "construction_gc", "type": "string", "description": "General contractor from macro_combined."}, {"column": "attempted_reviews", "type": "int", "description": "COUNT(a.smp_id) over hse_table after filters; non-distinct review-row count."}, {"column": "compliant_reviews", "type": "int", "description": "Review rows where LOWER(non_compliance_status) = \'ok\'."}, {"column": "compliance_rate_pct", "type": "float", "description": "100 * compliant_reviews / attempted_reviews."}, {"column": "non_compliant_count", "type": "int", "description": "Review rows where LOWER(non_compliance_status) = \'not ok\'."}, {"column": "fail_check_in_count", "type": "int", "description": "Review rows where LOWER(check_in_status) = \'not done\'."}, {"column": "fail_ppe_count", "type": "int", "description": "Review rows where LOWER(ppe_validation) IN (\'fail\',\'failed\')."}, {"column": "fail_jsa_count", "type": "int", "description": "Review rows where LOWER(jsa_completion_status) = \'not completed\'."}, {"column": "fail_ptid_l2w_count", "type": "int", "description": "Review rows where LOWER(each_crew_ptid_l2w_status) = \'not available\'."}]',
#     n.kpi_filters = '[{"name": "start_date", "type": "date", "description": "Inside hse_table: gsd_hse_review_check_date::date >= DATE \'start_date\'."}, {"name": "end_date", "type": "date", "description": "Inside hse_table: gsd_hse_review_check_date::date < DATE \'end_date\'."}, {"name": "smp_name", "type": "string", "description": "Inside hse_table: filters by checklist\'s smp_name (e.g., \'NTM\', \'AHLOB Modernization\')."}, {"name": "rgn_region", "type": "string", "description": "Outer WHERE on b.rgn_region (post-JOIN; combined_table stays unfiltered)."}, {"name": "m_area", "type": "string", "description": "Outer WHERE on b.m_area."}, {"name": "m_market", "type": "string", "description": "Outer WHERE on b.m_market."}, {"name": "construction_gc", "type": "string", "description": "Outer WHERE on b.construction_gc."}, {"name": "smp_id", "type": "string", "description": "Outer WHERE on b.smp_id."}, {"name": "customer_name", "type": "string", "description": "Inside hse_table: filters by customer_name."}, {"name": "hse_status", "type": "string", "description": "Inside hse_table: filters by hse_status."}, {"name": "non_compliance_status", "type": "string", "description": "Inside hse_table: filters by non_compliance_status (case-sensitive equality; \'Ok\' / \'Not Ok\')."}, {"name": "non_complaince_category", "type": "string", "description": "Inside hse_table: filters by non_complaince_category (column name preserves source spelling)."}, {"name": "hse_engineer_name", "type": "string", "description": "Inside hse_table: filters by hse_engineer_name."}]',
#     n.kpi_contract = '{"function_name": "get_hse_compliance_rate", "node_type": "kpi", "node_id": "313756f8-da42-470a-998f-dc558b1940f3", "node_label": "H&S/HSE Compliance Rate", "description": "Percent of HSE reviews that pass H&S/HSE compliance, computed at non-distinct review-row grain and rolled up by macro project geo dims (rgn_region, m_area, m_market, construction_gc). Two-CTE structure mirrors client query: `hse_table` carries all row-level filters (3 mandatory NOT NULL filters + optional date range, smp_name, and checklist-side filters); `combined_table` is `SELECT DISTINCT smp_id, geo_dims FROM macro_combined` with NO WHERE clause. Inner JOIN on smp_id; optional geo filters apply in the outer WHERE.\\n\\nattempted_reviews = COUNT(a.smp_id) (NON-distinct).\\ncompliant_reviews = COUNT(CASE WHEN LOWER(non_compliance_status) = \'ok\' THEN smp_id END).\\ncompliance_rate_pct = 100.0 * compliant_reviews / NULLIF(attempted_reviews, 0).\\n\\nPer-gate diagnostic fail counts (also non-distinct, LOWER-matched):\\n- non_compliant_count: LOWER(non_compliance_status) = \'not ok\'\\n- fail_check_in_count: LOWER(check_in_status) = \'not done\'\\n- fail_ppe_count: LOWER(ppe_validation) IN (\'fail\',\'failed\')\\n- fail_jsa_count: LOWER(jsa_completion_status) = \'not completed\'\\n- fail_ptid_l2w_count: LOWER(each_crew_ptid_l2w_status) = \'not available\'\\n\\nH&S and HSE are synonyms - both refer to this same KPI.", "parameters": [{"name": "start_date", "type": "date", "description": "hse_table WHERE: gsd_hse_review_check_date::date >= DATE.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "hse_table WHERE: gsd_hse_review_check_date::date < DATE.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "hse_table WHERE on checklist smp_name (e.g., \'NTM\', \'AHLOB Modernization\').", "required": false, "sample_values": ["NTM", "AHLOB Modernization"]}, {"name": "rgn_region", "type": "string", "description": "Outer WHERE on b.rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Outer WHERE on b.m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Outer WHERE on b.m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Outer WHERE on b.construction_gc.", "required": false, "sample_values": []}, {"name": "non_compliance_status", "type": "string", "description": "hse_table WHERE on non_compliance_status (\'Ok\' or \'Not Ok\').", "required": false, "sample_values": ["Ok", "Not Ok"]}, {"name": "non_complaince_category", "type": "string", "description": "hse_table WHERE on non_complaince_category.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": true}, {"name": "m_area", "type": "string", "nullable": true}, {"name": "m_market", "type": "string", "nullable": true}, {"name": "construction_gc", "type": "string", "nullable": true}, {"name": "attempted_reviews", "type": "number", "nullable": false}, {"name": "compliant_reviews", "type": "number", "nullable": false}, {"name": "compliance_rate_pct", "type": "number", "nullable": true}, {"name": "non_compliant_count", "type": "number", "nullable": false}, {"name": "fail_check_in_count", "type": "number", "nullable": false}, {"name": "fail_ppe_count", "type": "number", "nullable": false}, {"name": "fail_jsa_count", "type": "number", "nullable": false}, {"name": "fail_ptid_l2w_count", "type": "number", "nullable": false}], "sample_output": [], "row_count": 10}',
#     n.nl_description = 'H&S/HSE Compliance Rate = 100 * (review rows with LOWER(non_compliance_status) = \'ok\') / (review rows with all three of gsd_hse_review_check_date, check_in_status, non_compliance_status NOT NULL), grouped by (rgn_region, m_area, m_market, construction_gc). Built via two CTEs: hse_table (checklist + filters) and combined_table (SELECT DISTINCT smp_id, geo_dims FROM macro - no WHERE). INNER JOIN on smp_id. Counts are NON-distinct (each surviving review row counts). Includes diagnostic per-gate fail counts (PPE, JSA, PTID L2W, check-in). H&S and HSE are synonyms.',
#     n.definition = 'H&S/HSE Compliance Rate = 100 * (review rows with LOWER(non_compliance_status) = \'ok\') / (review rows with all three of gsd_hse_review_check_date, check_in_status, non_compliance_status NOT NULL), grouped by (rgn_region, m_area, m_market, construction_gc). Built via two CTEs: hse_table (checklist + filters) and combined_table (SELECT DISTINCT smp_id, geo_dims FROM macro - no WHERE). INNER JOIN on smp_id. Counts are NON-distinct (each surviving review row counts). Includes diagnostic per-gate fail counts (PPE, JSA, PTID L2W, check-in). H&S and HSE are synonyms.',
#     n.kpi_description = 'Percent of HSE reviews that pass H&S/HSE compliance at review-row grain, rolled up by macro project geo dims (rgn_region, m_area, m_market, construction_gc). Built via two CTEs: hse_table holds the filtered checklist rows (3 mandatory NOT NULL filters: gsd_hse_review_check_date, check_in_status, non_compliance_status; plus optional smp_name (checklist-side), date range, and other checklist filters); combined_table is SELECT DISTINCT smp_id + geo dims from macro_combined with NO WHERE. INNER JOIN on smp_id. Counts are NON-distinct so the same project can contribute multiple review rows. compliant_reviews uses LOWER(non_compliance_status) = \'ok\'. Per-gate diagnostic fail counts use LOWER-matched values for \'Not Done\', \'Fail/Failed\', \'Not Completed\', \'Not Available\'. H&S and HSE are synonyms.',
#     n.kpi_source_columns = ['stg_ndpd_hse_site_checklist.smp_id', 'stg_ndpd_hse_site_checklist.smp_name', 'stg_ndpd_hse_site_checklist.gsd_hse_review_check_date', 'stg_ndpd_hse_site_checklist.check_in_status', 'stg_ndpd_hse_site_checklist.ppe_validation', 'stg_ndpd_hse_site_checklist.jsa_completion_status', 'stg_ndpd_hse_site_checklist.each_crew_ptid_l2w_status', 'stg_ndpd_hse_site_checklist.non_compliance_status', 'stg_ndpd_hse_site_checklist.non_complaince_category', 'stg_ndpd_hse_site_checklist.customer_name', 'stg_ndpd_hse_site_checklist.hse_status', 'stg_ndpd_hse_site_checklist.hse_engineer_name', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc']
# RETURN n.label AS label, n.node_id AS node_id;