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

# MATCH (n:BKGNode {node_id: 'cpf-001-construction-plan-forecast'})
# WITH n, n.kpi_python_function AS fn_before
# SET n.kpi_python_function = replace(
#     n.kpi_python_function,
#     'ms_1555_construction_complete_actual IS NULL"\n            " AND s_site_id IS NOT NULL" +\n            smp_filter + user_filter\n        )\n        pending_rows = execute_query(cnt_sql)',
#     'pj_a_4225_construction_start_finish IS NOT NULL"\n            " AND pj_a_5175_construction_complete_finish IS NULL"\n            " AND s_site_id IS NOT NULL" +\n            smp_filter + user_filter\n        )\n        pending_rows = execute_query(cnt_sql)'
# )
# WITH n, fn_before
# RETURN
#   size(fn_before)                                                    AS old_fn_chars,
#   size(n.kpi_python_function)                                        AS new_fn_chars,
#   size(n.kpi_python_function) - size(fn_before)                      AS delta_chars,
#   n.kpi_python_function CONTAINS 'pj_a_4225_construction_start_finish IS NOT NULL'
#     AND n.kpi_python_function CONTAINS 'pj_a_5175_construction_complete_finish IS NULL'
#                                                                       AS pending_definition_applied;