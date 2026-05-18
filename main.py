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


Business Knowledge Graph (BKG) — at a glance
What it is. The BKG is the "map" our agent uses to understand the business. Instead of asking the agent to remember every table, column, and formula, we draw the business as a network of nodes (things) connected by relationships (how they relate). The agent navigates this network to answer a question.

Two kinds of nodes:

Node type	What it is	Example
Core node	A real-world thing in the business — a noun. It anchors the domain.	Project, Site, General Contractor, Market, Closeout Package
KPI node	A measurement — a number the business cares about. It pulls data from one or more core nodes and computes a result.	CX to On-Air Backlog, SCOP/COP Quality Rate, Customer & Nokia Approval & Rejection Rate
A KPI is wired to the core nodes it depends on (e.g., SCOP/COP Quality Rate connects to Project, Site, Market, General Contractor, Closeout Package). When the agent gets a question, it picks the right KPI by following these connections.

What we built in the last 3 months
1) Smarter retrieval — far less wasted effort
We tried two approaches for handing the graph to the agent:

Approach A — Brute force (schema dump).
For every sub-question, we handed the agent the entire graph schema — every node, every relationship, every definition. That was ~80,000 tokens of context per sub-query. Most of it was irrelevant noise the agent had to read through.

Approach B — Optimal (embedding-based retrieval).
We pre-computed an "embedding" (a numerical fingerprint of meaning) for every path up to 3 hops out from each node, and stored them in a lookup table. When a question comes in, we search those fingerprints and hand the agent only the paths that actually look relevant.

Result: ~80K → ~5K tokens per sub-query — a ~16× reduction. The agent thinks faster, costs less to run, and gets less distracted by irrelevant context.

2) Logical refinements that improved data quality
Geography filter guard. All geo filters (region/area/market/GC) now require IS NOT NULL — rows with missing geography no longer pollute regional roll-ups.
Cleaner KPI definitions. Several KPIs (e.g., CX to On-Air Backlog, Planned Sites Count) were simplified to use a single canonical date column instead of legacy fallback chains, removing duplicate-counting and ambiguity.
HSE compliance rules tightened. Refined which projects are counted as in-scope, eliminating false negatives.
Removed a redundant core node. The old Punch Checklist node duplicated Closeout Package and confused the retrieval agent on "punch"-style keywords. Merged the workflow edges onto Closeout Package and dropped the duplicate.
Open, consistent filter surfaces. KPIs now accept the same standard set of optional filters (program, region, area, market, GC, project, site, date range), so the agent can mix and match without special-cased branching.
New KPIs for the customer/Nokia review cycle. Added Customer & Nokia Approval & Rejection Rate and Customer & Nokia Punch Point Count to measure quality-review outcomes from both sides of the workflow — previously only the site-level FTR rate was visible.