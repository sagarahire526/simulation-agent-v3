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


# MATCH (n:BKGNode { session_id: '69a3d22f26e208edc083a06e', node_id: '64a36227-713c-4a1a-9f7d-79079c625345' })
# SET n.kpi_python_function = 'def get_crew_capacity(execute_query, filters=None) -> list[dict]:\n    filters = filters or {}\n    smp_name_raw = filters.get("smp_name")\n    smp_upper = str(smp_name_raw).strip().upper() if smp_name_raw else None\n\n    proj_filters = [\'t2."construction_gc" IS NOT NULL\']\n    if filters.get("rgn_region"):\n        v = str(filters["rgn_region"]).replace("\'", "\'\'")\n        proj_filters.append(f"t2.\\"rgn_region\\" = \'{v}\'")\n    if filters.get("m_area"):\n        v = str(filters["m_area"]).replace("\'", "\'\'")\n        proj_filters.append(f"t2.\\"m_area\\" = \'{v}\'")\n    if filters.get("m_market"):\n        v = str(filters["m_market"]).replace("\'", "\'\'")\n        proj_filters.append(f"t2.\\"m_market\\" = \'{v}\'")\n    if filters.get("construction_gc"):\n        v = str(filters["construction_gc"]).replace("\'", "\'\'")\n        proj_filters.append(f"t2.\\"construction_gc\\" = \'{v}\'")\n\n    if smp_upper == "NTM":\n        # NTM: average distinct crew leads across 4 batches (10-day windows over last 39 days)\n        proj_filters.append("t2.\\"smp_name\\" = \'NTM\'")\n        proj_where = " AND ".join(proj_filters)\n        sql = f"""\n            WITH hse AS (\n                SELECT DISTINCT\n                    "smp_id",\n                    RTRIM(LOWER("crew_lead_name")) AS "crew_lead_name",\n                    "check_in_date",\n                    CASE\n                        WHEN "check_in_date" >= CURRENT_DATE - INTERVAL \'9 days\'\n                            THEN \'Batch-1\'\n                        WHEN "check_in_date" >= CURRENT_DATE - INTERVAL \'19 days\'\n                             AND "check_in_date" < CURRENT_DATE - INTERVAL \'9 days\'\n                            THEN \'Batch-2\'\n                        WHEN "check_in_date" >= CURRENT_DATE - INTERVAL \'29 days\'\n                             AND "check_in_date" < CURRENT_DATE - INTERVAL \'19 days\'\n                            THEN \'Batch-3\'\n                        WHEN "check_in_date" >= CURRENT_DATE - INTERVAL \'39 days\'\n                             AND "check_in_date" < CURRENT_DATE - INTERVAL \'29 days\'\n                            THEN \'Batch-4\'\n                    END AS batch\n                FROM pwc_macro_staging_schema.stg_tmo_hse_daily_tracker_v0_1\n                WHERE "crew_lead_name" IS NOT NULL\n                  AND "check_in_date" >= CURRENT_DATE - INTERVAL \'39 days\'\n            ),\n            proj AS (\n                SELECT DISTINCT\n                    t2."smp_id",\n                    t2."rgn_region",\n                    t2."m_area",\n                    t2."m_market",\n                    t2."construction_gc"\n                FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined t2\n                WHERE {proj_where}\n            ),\n            batch_counts AS (\n                SELECT\n                    p."rgn_region",\n                    p."m_area",\n                    p."m_market",\n                    p."construction_gc",\n                    h.batch,\n                    COUNT(DISTINCT h."crew_lead_name") AS crew_lead_count\n                FROM hse h\n                INNER JOIN proj p ON h."smp_id" = p."smp_id"\n                WHERE h.batch IS NOT NULL\n                GROUP BY\n                    p."rgn_region",\n                    p."m_area",\n                    p."m_market",\n                    p."construction_gc",\n                    h.batch\n            )\n            SELECT\n                "rgn_region",\n                "m_area",\n                "m_market",\n                "construction_gc",\n                AVG(crew_lead_count) AS "crew_lead_count"\n            FROM batch_counts\n            GROUP BY "rgn_region", "m_area", "m_market", "construction_gc"\n            ORDER BY "rgn_region", "m_area", "m_market", "construction_gc"\n        """\n        return execute_query(sql)\n\n    if smp_upper == "AHLOB MODERNIZATION":\n        # AHLOB Modernization: distinct crew leads observed in the last 7 days\n        proj_filters.append("t2.\\"smp_name\\" = \'AHLOB Modernization\'")\n        proj_where = " AND ".join(proj_filters)\n        sql = f"""\n            SELECT\n                t2."rgn_region",\n                t2."m_area",\n                t2."m_market",\n                t2."construction_gc",\n                COUNT(DISTINCT RTRIM(t1."crew_lead_name")) AS "crew_lead_count"\n            FROM pwc_macro_staging_schema.stg_tmo_hse_daily_tracker_v0_1 t1\n            INNER JOIN pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined t2\n                ON t1."smp_id" = t2."smp_id"\n            WHERE {proj_where}\n              AND t1."check_in_date" >= CURRENT_DATE - INTERVAL \'7 days\'\n              AND RTRIM(t1."crew_lead_name") IS NOT NULL\n            GROUP BY t2."rgn_region", t2."m_area", t2."m_market", t2."construction_gc"\n            ORDER BY t2."rgn_region", t2."m_area", t2."m_market", t2."construction_gc"\n        """\n        return execute_query(sql)\n\n    # Default: distinct crew leads per GC, no date window\n    where_parts = [\n        "1=1",\n        \'RTRIM(t1."crew_lead_name") IS NOT NULL\',\n        \'t2."construction_gc" IS NOT NULL\',\n    ]\n    if filters.get("rgn_region"):\n        v = str(filters["rgn_region"]).replace("\'", "\'\'")\n        where_parts.append(f"t2.\\"rgn_region\\" = \'{v}\'")\n    if filters.get("m_area"):\n        v = str(filters["m_area"]).replace("\'", "\'\'")\n        where_parts.append(f"t2.\\"m_area\\" = \'{v}\'")\n    if filters.get("m_market"):\n        v = str(filters["m_market"]).replace("\'", "\'\'")\n        where_parts.append(f"t2.\\"m_market\\" = \'{v}\'")\n    if filters.get("construction_gc"):\n        v = str(filters["construction_gc"]).replace("\'", "\'\'")\n        where_parts.append(f"t2.\\"construction_gc\\" = \'{v}\'")\n    if smp_name_raw:\n        v = str(smp_name_raw).replace("\'", "\'\'")\n        where_parts.append(f"t2.\\"smp_name\\" = \'{v}\'")\n    where_sql = " AND ".join(where_parts)\n    sql = f"""\n        SELECT\n            t2."rgn_region",\n            t2."m_area",\n            t2."m_market",\n            t2."construction_gc",\n            COUNT(DISTINCT RTRIM(t1."crew_lead_name")) AS "crew_lead_count"\n        FROM pwc_macro_staging_schema.stg_tmo_hse_daily_tracker_v0_1 t1\n        INNER JOIN pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined t2\n            ON t1."smp_id" = t2."smp_id"\n        WHERE {where_sql}\n        GROUP BY t2."rgn_region", t2."m_area", t2."m_market", t2."construction_gc"\n        ORDER BY t2."rgn_region", t2."m_area", t2."m_market", t2."construction_gc"\n    """\n    return execute_query(sql)'
# RETURN n.label AS label, n.node_id AS node_id;

# MATCH (n:BKGNode { session_id: '69a3d22f26e208edc083a06e', node_id: '30e3c50e-a44c-4e27-8153-71741e2507d0' })
# SET n.kpi_python_function = 'def get_site_revisit_rate(execute_query, filters=None) -> list[dict]:\n    filters = filters or {}\n\n    where_parts = [\'a."pj_a_5175_construction_complete_finish" IS NOT NULL\']\n    if filters.get("smp_name"):\n        v = str(filters["smp_name"]).replace("\'", "\'\'")\n        where_parts.append(f"a.\\"smp_name\\" = \'{v}\'")\n    if filters.get("rgn_region"):\n        v = str(filters["rgn_region"]).replace("\'", "\'\'")\n        where_parts.append(f"a.\\"rgn_region\\" = \'{v}\'")\n    if filters.get("m_area"):\n        v = str(filters["m_area"]).replace("\'", "\'\'")\n        where_parts.append(f"a.\\"m_area\\" = \'{v}\'")\n    if filters.get("m_market"):\n        v = str(filters["m_market"]).replace("\'", "\'\'")\n        where_parts.append(f"a.\\"m_market\\" = \'{v}\'")\n    if filters.get("construction_gc"):\n        v = str(filters["construction_gc"]).replace("\'", "\'\'")\n        where_parts.append(f"a.\\"construction_gc\\" = \'{v}\'")\n    base_where = " AND ".join(where_parts)\n\n    sql = f"""\n        WITH revisit_sites AS (\n            SELECT\n                a."rgn_region",\n                a."m_area",\n                a."m_market",\n                a."construction_gc",\n                COUNT(DISTINCT a."pj_project_id") AS revisit_reschedule_count\n            FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined a\n            JOIN public.tmo_macro_copilot_ahloa_reschedule_delay_code b\n                ON a."s_site_id" = b."mb_s_site_code"\n            WHERE {base_where}\n              AND b."status" IS NOT NULL\n            GROUP BY a."rgn_region", a."m_area", a."m_market", a."construction_gc"\n        ),\n        total_sites AS (\n            SELECT\n                a."rgn_region",\n                a."m_area",\n                a."m_market",\n                a."construction_gc",\n                COUNT(DISTINCT a."pj_project_id") AS total_count\n            FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined a\n            WHERE {base_where}\n            GROUP BY a."rgn_region", a."m_area", a."m_market", a."construction_gc"\n        )\n        SELECT\n            t."rgn_region" AS "rgn_region",\n            t."m_area" AS "m_area",\n            t."m_market" AS "m_market",\n            t."construction_gc" AS "construction_gc",\n            ROUND(\n                100.0 * COALESCE(r.revisit_reschedule_count, 0)\n                      / NULLIF(t.total_count, 0),\n                2\n            ) AS "revisit_reschedule_percentage"\n        FROM total_sites t\n        LEFT JOIN revisit_sites r\n            ON COALESCE(t."rgn_region", \'\') = COALESCE(r."rgn_region", \'\')\n           AND COALESCE(t."m_area", \'\') = COALESCE(r."m_area", \'\')\n           AND COALESCE(t."m_market", \'\') = COALESCE(r."m_market", \'\')\n           AND COALESCE(t."construction_gc", \'\') = COALESCE(r."construction_gc", \'\')\n        ORDER BY t."rgn_region", t."m_area", t."m_market", t."construction_gc"\n    """\n    return execute_query(sql)'
# RETURN n.label AS label, n.node_id AaS node_id;