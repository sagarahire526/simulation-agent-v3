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



# kpi_python_function: "def get_swap_ftr(execute_query, filters=None) -> list[dict]:
#     where_parts = [
#         "1=1",
#         "t1.\"pj_a_5175_construction_complete_finish\" IS NOT NULL",
#         "COALESCE(t1.\"pj_project_status\", '') <> 'Dead'",
#         "TRIM(COALESCE(t2.\"check_in_date\", '')) <> ''"
#     ]

#     # Explicit allowlist filters
#     if filters:
#         if filters.get("rgn_region"):
#             v = str(filters["rgn_region"]).replace("'", "''")
#             where_parts.append(f"t1.\"rgn_region\" = '{v}'")
#         if filters.get("m_area"):
#             v = str(filters["m_area"]).replace("'", "''")
#             where_parts.append(f"t1.\"m_area\" = '{v}'")
#         if filters.get("m_market"):
#             v = str(filters["m_market"]).replace("'", "''")
#             where_parts.append(f"t1.\"m_market\" = '{v}'")
#         if filters.get("construction_gc"):
#             v = str(filters["construction_gc"]).replace("'", "''")
#             where_parts.append(f"t1.\"construction_gc\" = '{v}'")
#         if filters.get("smp_name"):
#             v = str(filters["smp_name"]).replace("'", "''")
#             where_parts.append(f"t1.\"smp_name\" = '{v}'")

#     where_sql = " AND ".join(where_parts)

#     sql = f"""
#     SELECT
#       base.\"rgn_region\" AS \"rgn_region\",
#       base.\"m_area\" AS \"m_area\",
#       base.\"m_market\" AS \"m_market\",
#       base.\"construction_gc\" AS \"construction_gc\",
#       COUNT(DISTINCT base.\"smp_id\") AS \"total_swap_sites\",
#       COUNT(DISTINCT CASE WHEN base.\"hse_visit_count\" = 1 THEN base.\"smp_id\" END) AS \"ftr_sites\",
#       (
#         COUNT(DISTINCT base.\"smp_id\")
#         - COUNT(DISTINCT CASE WHEN base.\"hse_visit_count\" = 1 THEN base.\"smp_id\" END)
#       ) AS \"non_ftr_sites\",
#       100.0
#         * COUNT(DISTINCT CASE WHEN base.\"hse_visit_count\" = 1 THEN base.\"smp_id\" END)
#         / NULLIF(COUNT(DISTINCT base.\"smp_id\"), 0) AS \"ftr_rate_pct\",
#       AVG(base.\"hse_visit_count\"::numeric) AS \"avg_visits_per_site\"
#     FROM (
#       SELECT
#         t1.\"smp_id\" AS \"smp_id\",
#         t1.\"rgn_region\" AS \"rgn_region\",
#         t1.\"m_area\" AS \"m_area\",
#         t1.\"m_market\" AS \"m_market\",
#         t1.\"construction_gc\" AS \"construction_gc\",
#         COUNT(t2.\"id\") AS \"hse_visit_count\"
#       FROM \"public\".\"stg_ndpd_mbt_tmobile_macro_combined\" t1
#       INNER JOIN \"public\".\"stg_ndpd_hse_site_checklist\" t2
#         ON t1.\"smp_id\" = t2.\"smp_id\"
#       WHERE {where_sql}
#       GROUP BY
#         t1.\"smp_id\",
#         t1.\"rgn_region\",
#         t1.\"m_area\",
#         t1.\"m_market\",
#         t1.\"construction_gc\"
#     ) base
#     GROUP BY
#       base.\"rgn_region\",
#       base.\"m_area\",
#       base.\"m_market\",
#       base.\"construction_gc\"
#     ORDER BY
#       base.\"rgn_region\",
#       base.\"m_area\",
#       base.\"m_market\",
#       base.\"construction_gc\";
#     """

#     return execute_query(sql, db="public")
# "