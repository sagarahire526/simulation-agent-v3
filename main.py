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


# // Step 1: Delete punch_checklist + all its connected edges
# MATCH (n:BKGNode {
#     session_id: '69a3d22f26e208edc083a06e',
#     node_id: 'punch_checklist'
# })
# DETACH DELETE n;


# // Step 2a: Create FEEDS_INTO relationship
# // quality_session_ndpc -> closeout_package

# MATCH (a:BKGNode {
#         session_id: '69a3d22f26e208edc083a06e',
#         node_id: 'quality_session_ndpc'
#       }),
#       (b:BKGNode {
#         session_id: '69a3d22f26e208edc083a06e',
#         node_id: 'closeout_package'
#       })

# WHERE NOT EXISTS {
#     MATCH (a)-[r:RELATES_TO]->(b)
#     WHERE r.relationship_type = 'FEEDS_INTO'
# }

# CREATE (a)-[:RELATES_TO {
#     relationship_type: 'FEEDS_INTO',
#     edge_id:           '6629d1cc-755a-4fb6-ad2c-d4659339c31f',
#     session_id:        '69a3d22f26e208edc083a06e',
#     style:             'solid',
#     relationship:      'feeds_into',
#     status:            'confirmed'
# }]->(b);


# // Step 2b: Create REQUIRES relationship
# // closeout_package -> integration_activity

# MATCH (a:BKGNode {
#         session_id: '69a3d22f26e208edc083a06e',
#         node_id: 'closeout_package'
#       }),
#       (b:BKGNode {
#         session_id: '69a3d22f26e208edc083a06e',
#         node_id: 'integration_activity'
#       })

# WHERE NOT EXISTS {
#     MATCH (a)-[r:RELATES_TO]->(b)
#     WHERE r.relationship_type = 'REQUIRES'
# }

# CREATE (a)-[:RELATES_TO {
#     relationship_type: 'REQUIRES',
#     edge_id:           'c521cf25-4a5b-495e-b161-772452d82063',
#     session_id:        '69a3d22f26e208edc083a06e',
#     style:             'solid',
#     relationship:      'requires',
#     status:            'confirmed'
# }]->(b);


# // Step 3: Verification queries

# // Verify node deletion
# MATCH (n:BKGNode {
#     session_id: '69a3d22f26e208edc083a06e',
#     node_id: 'punch_checklist'
# })
# RETURN count(n) AS punch_checklist_count;


# // Verify closeout_package edge count
# MATCH (n:BKGNode {
#     session_id: '69a3d22f26e208edc083a06e',
#     node_id: 'closeout_package'
# })-[r:RELATES_TO]-()
# RETURN count(r) AS closeout_package_edge_count;