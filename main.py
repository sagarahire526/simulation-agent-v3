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



# // Idempotent update — adds per-program window logic to Crew Capacity's
# // definition and fills in nl_business_rule (was empty).
# // Safe to re-run; no-op if the node doesn't exist.
# MATCH (n:BKGNode { session_id: '69a3d22f26e208edc083a06e', node_id: '64a36227-713c-4a1a-9f7d-79079c625345' })
# SET
#     n.definition       = 'Crew Capacity = COUNT(DISTINCT HSE crew_lead_name) per (rgn_region, m_area, m_market, construction_gc) after INNER JOINing the HSE daily tracker to macro projects on smp_id and excluding NULL crew leads and NULL GCs. The window over which distinct crew leads are counted depends on the program (smp_name): for \'AHLOB Modernization\' only the last 7 days of HSE check-ins are used; for \'NTM\' the last 39 days are split into four 10-day batches and the per-batch distinct crew-lead counts are averaged across batches; if no smp_name is supplied, no date window is applied.',
#     n.nl_business_rule = 'The crew-counting window is program-specific (driven by smp_name):\n\n1) AHLOB Modernization — last-7-days view.\n   a) From stg_tmo_hse_daily_tracker_v0_1, keep rows where crew_lead_name IS NOT NULL and check_in_date >= CURRENT_DATE - INTERVAL \'7 days\'.\n   b) INNER JOIN to stg_ndpd_mbt_tmobile_macro_combined on smp_id, filtered to smp_name = \'AHLOB Modernization\' with construction_gc IS NOT NULL.\n   c) For each (rgn_region, m_area, m_market, construction_gc) group, compute crew_lead_count = COUNT(DISTINCT crew_lead_name).\n\n2) NTM — 4 x 10-day batch average over the last 39 days.\n   a) From stg_tmo_hse_daily_tracker_v0_1, keep rows where crew_lead_name IS NOT NULL and check_in_date >= CURRENT_DATE - INTERVAL \'39 days\'.\n   b) Bucket each check-in into one of four 10-day batches:\n        Batch-1: check_in_date >= CURRENT_DATE - INTERVAL \'9 days\'\n        Batch-2: check_in_date in [CURRENT_DATE - 19 days, CURRENT_DATE - 9 days)\n        Batch-3: check_in_date in [CURRENT_DATE - 29 days, CURRENT_DATE - 19 days)\n        Batch-4: check_in_date in [CURRENT_DATE - 39 days, CURRENT_DATE - 29 days)\n   c) INNER JOIN to stg_ndpd_mbt_tmobile_macro_combined on smp_id, filtered to smp_name = \'NTM\' with construction_gc IS NOT NULL.\n   d) For each (rgn_region, m_area, m_market, construction_gc, batch) group, compute distinct crew-lead count = COUNT(DISTINCT crew_lead_name).\n   e) Average the per-batch counts per (rgn_region, m_area, m_market, construction_gc) — that average is crew_lead_count for the group.\n\n3) Default (no smp_name supplied or any other value) — no date window is applied. Compute COUNT(DISTINCT crew_lead_name) over all HSE check-ins per (rgn_region, m_area, m_market, construction_gc), with crew_lead_name and construction_gc both required to be NOT NULL.\n\nOptional dimension filters (rgn_region, m_area, m_market, construction_gc) are applied to the macro side. The smp_name filter routes the calculation between the three branches above.'
# RETURN n.label AS label, n.node_id AS node_id;
