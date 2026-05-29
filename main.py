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


# Step A — drop Rejection Record
# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'rejection_record'})
# DETACH DELETE n;


# Step B — update Customer & Nokia Punch Point Count
# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: '5cc3d858-db28-46fb-920a-6439c9dc2c7b'})
# SET
#     n.kpi_python_function = 'def get_customer_nokia_punch_point_count(execute_query, filters=None) -> list[dict]:\n    """Customer & Nokia Punch Point Count — with per-reason breakdown.\n\n    Answer-level volume of punch points by source AND reason:\n      - Customer punch point  = a."formanswerstatus" = \'7\'        (T-Mobile rejected)\n      - Nokia punch point     = a."formanswerstatus" IN (\'2\',\'4\') (Nokia rejected OR Nokia AI flagged)\n\n    The reason behind each punch point is exposed via a LEFT JOIN to\n    pwc_macro_staging_schema.stg_tmo_rejections_master_file (t) on\n    t."trmf_smp_id" = b."smp_id", surfaced as `punch_point_reason` in the output\n    and added to GROUP BY.\n\n    Because the trmf side is one-smp-to-many-rejections, COUNT(DISTINCT a."id")\n    is used so the LEFT JOIN cannot inflate counts (each answer is still counted\n    once per (reason) bucket it falls into). Rows whose smp has no trmf record\n    are kept with punch_point_reason = NULL.\n\n    formanswerstatus value map:\n      \'0\' = submitted\n      \'1\' = Nokia approved (human)\n      \'2\' = Nokia rejected\n      \'3\' = not submitted\n      \'4\' = Nokia AI approved\n      \'6\' = customer approved (T-Mobile)\n      \'7\' = customer rejected (T-Mobile)\n\n    Source:\n      pwc_macro_staging_schema.stg_services_ndpc_answers_daily_from_sdb a\n      JOIN pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined b\n        ON a."sessiontitle" = b."scop_title"\n      LEFT JOIN pwc_macro_staging_schema.stg_tmo_rejections_master_file t\n        ON t."trmf_smp_id" = b."smp_id"\n\n    Output (one row per rgn_region × m_area × m_market × construction_gc × punch_point_reason):\n      rgn_region, m_area, m_market, construction_gc, punch_point_reason,\n      customer_punch_point_count, nokia_punch_point_count\n\n    Optional filters (all equality, all optional):\n      smp_name, smp_id,\n      rgn_region, m_area, m_market, construction_gc,\n      pj_project_id, s_site_id, customer_site_code,\n      package_name, por_category,\n      scop_title,\n      start_date, end_date (applied to a."formanswerdate"::date).\n    """\n    filters = filters or {}\n\n    def _esc(v):\n        return str(v).replace("\'", "\'\'")\n\n    where_parts = ["a.\\"formanswerstatus\\" IN (\'2\',\'4\',\'7\')"]\n\n    optional_eq_cols = [\n        "smp_name",\n        "smp_id",\n        "rgn_region",\n        "m_area",\n        "m_market",\n        "construction_gc",\n        "pj_project_id",\n        "s_site_id",\n        "customer_site_code",\n        "package_name",\n        "por_category",\n    ]\n    for col in optional_eq_cols:\n        v = filters.get(col)\n        if v is not None and str(v) != "":\n            where_parts.append(f\'b."{col}" = \\\'{_esc(v)}\\\'\')\n\n    scop_title = filters.get("scop_title")\n    if scop_title is not None and str(scop_title) != "":\n        where_parts.append(f\'a."sessiontitle" = \\\'{_esc(scop_title)}\\\'\')\n\n    start_date = filters.get("start_date")\n    end_date = filters.get("end_date")\n    if start_date:\n        where_parts.append(f"a.\\"formanswerdate\\"::date >= DATE \'{_esc(start_date)}\'")\n    if end_date:\n        where_parts.append(f"a.\\"formanswerdate\\"::date <= DATE \'{_esc(end_date)}\'")\n\n    where_sql = " AND ".join(where_parts)\n\n    sql = f"""\n        SELECT\n            b."rgn_region",\n            b."m_area",\n            b."m_market",\n            b."construction_gc",\n            t."trmf_tmo_punch_point_details" AS punch_point_reason,\n\n            COUNT(DISTINCT CASE WHEN a."formanswerstatus" = \'7\'        THEN a."id" END) AS customer_punch_point_count,\n            COUNT(DISTINCT CASE WHEN a."formanswerstatus" IN (\'2\',\'4\') THEN a."id" END) AS nokia_punch_point_count\n\n        FROM pwc_macro_staging_schema.stg_services_ndpc_answers_daily_from_sdb a\n        JOIN pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined b\n            ON a."sessiontitle" = b."scop_title"\n        LEFT JOIN pwc_macro_staging_schema.stg_tmo_rejections_master_file t\n            ON t."trmf_smp_id" = b."smp_id"\n        WHERE {where_sql}\n        GROUP BY b."rgn_region", b."m_area", b."m_market", b."construction_gc", t."trmf_tmo_punch_point_details"\n        ORDER BY b."rgn_region", b."m_area", b."m_market", b."construction_gc", t."trmf_tmo_punch_point_details"\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_tables = ['pwc_macro_staging_schema.stg_services_ndpc_answers_daily_from_sdb', 'pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined', 'pwc_macro_staging_schema.stg_tmo_rejections_master_file'],
#     n.kpi_source_columns = ['stg_services_ndpc_answers_daily_from_sdb.id', 'stg_services_ndpc_answers_daily_from_sdb.sessiontitle', 'stg_services_ndpc_answers_daily_from_sdb.formanswerstatus', 'stg_services_ndpc_answers_daily_from_sdb.formanswerdate', 'stg_ndpd_mbt_tmobile_macro_combined.scop_title', 'stg_ndpd_mbt_tmobile_macro_combined.pj_project_id', 'stg_ndpd_mbt_tmobile_macro_combined.s_site_id', 'stg_ndpd_mbt_tmobile_macro_combined.customer_site_code', 'stg_ndpd_mbt_tmobile_macro_combined.smp_id', 'stg_ndpd_mbt_tmobile_macro_combined.smp_name', 'stg_ndpd_mbt_tmobile_macro_combined.rgn_region', 'stg_ndpd_mbt_tmobile_macro_combined.m_area', 'stg_ndpd_mbt_tmobile_macro_combined.m_market', 'stg_ndpd_mbt_tmobile_macro_combined.construction_gc', 'stg_ndpd_mbt_tmobile_macro_combined.package_name', 'stg_ndpd_mbt_tmobile_macro_combined.por_category', 'stg_tmo_rejections_master_file.trmf_smp_id', 'stg_tmo_rejections_master_file.trmf_tmo_punch_point_details'],
#     n.kpi_filters = '[{"name": "smp_name", "type": "string", "description": "Optional equality filter on smp_name (program filter, e.g., NTM, AHLOB Modernization)."}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id."}, {"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region."}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area."}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market."}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc."}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id."}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id."}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code."}, {"name": "package_name", "type": "string", "description": "Optional equality filter on package_name."}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category."}, {"name": "scop_title", "type": "string", "description": "Optional equality filter on a.sessiontitle (the JOIN key)."}, {"name": "start_date", "type": "date", "description": "Lower bound on a.formanswerdate::date."}, {"name": "end_date", "type": "date", "description": "Upper bound on a.formanswerdate::date."}]',
#     n.kpi_contract = '{"function_name": "get_customer_nokia_punch_point_count", "node_type": "kpi", "node_id": "5cc3d858-db28-46fb-920a-6439c9dc2c7b", "node_label": "Customer & Nokia Punch Point Count", "description": "Answer-level punch-point volume by source with per-reason breakdown. Customer punch point = formanswerstatus = \'7\' (T-Mobile rejected). Nokia punch point = formanswerstatus IN (\'2\',\'4\') (Nokia rejected or Nokia AI flagged). Reasons are exposed via LEFT JOIN to stg_tmo_rejections_master_file on t.trmf_smp_id = b.smp_id, surfaced as the `punch_point_reason` output column and added to GROUP BY; counts use COUNT(DISTINCT a.id) so the one-smp-to-many-rejections fan-out cannot inflate them. Source join: NDPc answers JOIN macro_combined ON sessiontitle = scop_title. Grouped by (rgn_region, m_area, m_market, construction_gc, punch_point_reason): customer_punch_point_count, nokia_punch_point_count. Paired with `Customer & Nokia Approval & Rejection Rate` (rate counterpart).", "parameters": [{"name": "smp_name", "type": "string", "description": "Optional equality filter on smp_name (program filter, e.g., NTM, AHLOB Modernization).", "required": false, "sample_values": []}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id.", "required": false, "sample_values": []}, {"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code.", "required": false, "sample_values": []}, {"name": "package_name", "type": "string", "description": "Optional equality filter on package_name.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category.", "required": false, "sample_values": []}, {"name": "scop_title", "type": "string", "description": "Optional equality filter on a.sessiontitle (the JOIN key).", "required": false, "sample_values": []}, {"name": "start_date", "type": "date", "description": "Lower bound on a.formanswerdate::date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on a.formanswerdate::date.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": false}, {"name": "m_area", "type": "string", "nullable": false}, {"name": "m_market", "type": "string", "nullable": false}, {"name": "construction_gc", "type": "string", "nullable": false}, {"name": "punch_point_reason", "type": "string", "nullable": true}, {"name": "customer_punch_point_count", "type": "number", "nullable": false}, {"name": "nokia_punch_point_count", "type": "number", "nullable": false}], "sample_output": [], "row_count": 25}',
#     n.kpi_business_logic = '1) Join ndpc answers (a) to macro_combined (b) ON a.sessiontitle = b.scop_title. 2) LEFT JOIN stg_tmo_rejections_master_file (t) on t.trmf_smp_id = b.smp_id to surface the rejection reason. 3) Restrict to a.formanswerstatus IN (\'2\',\'4\',\'7\') — the union of punch-point producing statuses. 4) Apply optional equality filters on the macro side (b.*): smp_name, smp_id, rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, package_name, por_category; scop_title applies to a.sessiontitle. 5) Optional start_date / end_date apply to a.formanswerdate::date. 6) Count punch points by source using DISTINCT on a.id so the LEFT JOIN\'s potential fan-out (one smp → many trmf rows) cannot inflate counts: customer_punch_point_count = COUNT(DISTINCT CASE WHEN a.formanswerstatus=\'7\' THEN a.id END); nokia_punch_point_count   = COUNT(DISTINCT CASE WHEN a.formanswerstatus IN (\'2\',\'4\') THEN a.id END). 7) GROUP BY (b.rgn_region, b.m_area, b.m_market, b.construction_gc, t.trmf_tmo_punch_point_details). 8) Rows whose smp has no trmf record are kept with punch_point_reason = NULL.',
#     n.kpi_formula_description = 'Per row (one per geo × reason): customer_punch_point_count = COUNT(DISTINCT CASE WHEN a.formanswerstatus = \'7\' THEN a.id END); nokia_punch_point_count   = COUNT(DISTINCT CASE WHEN a.formanswerstatus IN (\'2\',\'4\') THEN a.id END). Reason column = t.trmf_tmo_punch_point_details via LEFT JOIN on t.trmf_smp_id = b.smp_id. COUNT(DISTINCT a.id) keeps counts accurate under the one-smp-to-many-rejections fan-out. Grouped by (rgn_region, m_area, m_market, construction_gc, punch_point_reason).',
#     n.nl_description = 'Answer-level punch-point volume by source with per-reason breakdown. Customer punch point = formanswerstatus = \'7\' (T-Mobile rejected). Nokia punch point = formanswerstatus IN (\'2\',\'4\') (Nokia rejected or Nokia AI flagged). Reasons are exposed via LEFT JOIN to stg_tmo_rejections_master_file on t.trmf_smp_id = b.smp_id, surfaced as the `punch_point_reason` output column and added to GROUP BY; counts use COUNT(DISTINCT a.id) so the one-smp-to-many-rejections fan-out cannot inflate them. Source join: NDPc answers JOIN macro_combined ON sessiontitle = scop_title. Grouped by (rgn_region, m_area, m_market, construction_gc, punch_point_reason): customer_punch_point_count, nokia_punch_point_count. Paired with `Customer & Nokia Approval & Rejection Rate` (rate counterpart).',
#     n.definition = 'Answer-level punch-point volume by source with per-reason breakdown. Customer punch point = formanswerstatus = \'7\' (T-Mobile rejected). Nokia punch point = formanswerstatus IN (\'2\',\'4\') (Nokia rejected or Nokia AI flagged). Reasons are exposed via LEFT JOIN to stg_tmo_rejections_master_file on t.trmf_smp_id = b.smp_id, surfaced as the `punch_point_reason` output column and added to GROUP BY; counts use COUNT(DISTINCT a.id) so the one-smp-to-many-rejections fan-out cannot inflate them. Source join: NDPc answers JOIN macro_combined ON sessiontitle = scop_title. Grouped by (rgn_region, m_area, m_market, construction_gc, punch_point_reason): customer_punch_point_count, nokia_punch_point_count. Paired with `Customer & Nokia Approval & Rejection Rate` (rate counterpart).',
#     n.kpi_description = 'Answer-level punch-point volume by source with per-reason breakdown. Customer punch point = formanswerstatus = \'7\' (T-Mobile rejected). Nokia punch point = formanswerstatus IN (\'2\',\'4\') (Nokia rejected or Nokia AI flagged). Reasons are exposed via LEFT JOIN to stg_tmo_rejections_master_file on t.trmf_smp_id = b.smp_id, surfaced as the `punch_point_reason` output column and added to GROUP BY; counts use COUNT(DISTINCT a.id) so the one-smp-to-many-rejections fan-out cannot inflate them. Source join: NDPc answers JOIN macro_combined ON sessiontitle = scop_title. Grouped by (rgn_region, m_area, m_market, construction_gc, punch_point_reason): customer_punch_point_count, nokia_punch_point_count. Paired with `Customer & Nokia Approval & Rejection Rate` (rate counterpart).'
# RETURN n.label AS label, n.node_id AS node_id;
