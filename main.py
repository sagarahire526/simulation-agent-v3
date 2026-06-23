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


# MATCH (n:BKGNode {session_id: '69a3d22f26e208edc083a06e', node_id: 'cc575a78-fe9f-4cad-b364-75e3f32f2c04'})
# SET
#     n.definition = 'Material Hold Time by GC — measures how long each general contractor is CURRENTLY HOLDING material that has NOT YET reached the construction/swap milestone. This is a still-holding (open) snapshot, NOT a historical / closed-cycle metric.\n\nHARD CONSTRAINTS (the generated SQL MUST satisfy ALL three):\n  1. The chosen anchor column {cx_anchor_col} MUST be IS NULL (the milestone has not happened — material is still being held). Never write IS NOT NULL on the anchor.\n  2. hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date. Never subtract the anchor from pickup — the anchor is NULL by definition, so any subtraction with it would yield NULL.\n  3. Counts MUST use COUNT(DISTINCT pj_project_id). Never use s_site_id for counting.\n\nBy default the anchor is pj_a_4225_construction_start_finish (construction/swap start). When the user asks about hold until completion, the phase filter switches the anchor to pj_a_5175_construction_complete_finish (construction/swap complete). Reports project_count, avg_hold_days, max_hold_days, and 8-day SLA breach counts per region / area / market / GC / delay reason.',
#     n.kpi_business_logic = 'STEP 0 — HARD CONSTRAINTS (these are non-negotiable; the generated SQL MUST satisfy them): (a) {cx_anchor_col} IS NULL — projects must be STILL HOLDING (milestone has not happened). Never IS NOT NULL on the anchor. (b) hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date — never subtract the anchor from pickup; the anchor is NULL by definition so that arithmetic would always yield NULL. (c) Counts use COUNT(DISTINCT pj_project_id) — never s_site_id. STEP 1 — Source: pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined; no JOIN. STEP 2 — Phase filter selects {cx_anchor_col} + {delay_code_col}: phase=\'start\' (default) → {cx_anchor_col} = pj_a_4225_construction_start_finish; {delay_code_col} = pj_construction_start_delay_code. phase=\'complete\' → {cx_anchor_col} = pj_a_5175_construction_complete_finish; {delay_code_col} = pj_construction_complete_delay_code. STEP 3 — Population WHERE: pj_a_3925_msl_pickup_date_finish IS NOT NULL AND {cx_anchor_col} IS NULL. STEP 4 — Apply optional equality filters: rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id, customer_site_code, smp_id, por_category. STEP 5 — smp_name conditional: if smp_name = \'NTM\', enforce smp_status = \'Active\' and exclude ntm_project_type IN (\'E2E06 - NSD Cx Mgmt\', \'E2E07 - OL/MOD Cx Mgmt\'). STEP 6 — Optional date range start_date / end_date applied to pj_a_3925_msl_pickup_date_finish::date. STEP 7 — Aggregate per (rgn_region, m_area, m_market, construction_gc, delay_reason): project_count = COUNT(DISTINCT pj_project_id); avg_hold_days = ROUND(AVG(hold_days)::numeric, 0); max_hold_days = MAX(hold_days); sla_breach_count = COUNT(DISTINCT pj_project_id WHERE hold_days > 8); sla_breach_pct = ROUND(100.0 * sla_breach_count / NULLIF(project_count, 0), 2). STEP 8 — ORDER BY avg_hold_days DESC NULLS LAST, construction_gc, delay_reason.',
#     n.kpi_contract = '{"function_name": "get_material_hold_time_by_gc", "node_type": "kpi", "node_id": "cc575a78-fe9f-4cad-b364-75e3f32f2c04", "node_label": "Material Hold Time by GC", "description": "Material Hold Time by GC \\u2014 measures how long each general contractor is CURRENTLY HOLDING material that has NOT YET reached the construction/swap milestone. This is a still-holding (open) snapshot, NOT a historical / closed-cycle metric.\\n\\nHARD CONSTRAINTS (the generated SQL MUST satisfy ALL three):\\n  1. The chosen anchor column {cx_anchor_col} MUST be IS NULL (the milestone has not happened \\u2014 material is still being held). Never write IS NOT NULL on the anchor.\\n  2. hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date. Never subtract the anchor from pickup \\u2014 the anchor is NULL by definition, so any subtraction with it would yield NULL.\\n  3. Counts MUST use COUNT(DISTINCT pj_project_id). Never use s_site_id for counting.\\n\\nBy default the anchor is pj_a_4225_construction_start_finish (construction/swap start). When the user asks about hold until completion, the phase filter switches the anchor to pj_a_5175_construction_complete_finish (construction/swap complete). Reports project_count, avg_hold_days, max_hold_days, and 8-day SLA breach counts per region / area / market / GC / delay reason.", "parameters": [{"name": "rgn_region", "type": "string", "description": "Optional equality filter on rgn_region.", "required": false, "sample_values": []}, {"name": "m_area", "type": "string", "description": "Optional equality filter on m_area.", "required": false, "sample_values": []}, {"name": "m_market", "type": "string", "description": "Optional equality filter on m_market.", "required": false, "sample_values": []}, {"name": "construction_gc", "type": "string", "description": "Optional equality filter on construction_gc.", "required": false, "sample_values": []}, {"name": "pj_project_id", "type": "string", "description": "Optional equality filter on pj_project_id.", "required": false, "sample_values": []}, {"name": "s_site_id", "type": "string", "description": "Optional equality filter on s_site_id.", "required": false, "sample_values": []}, {"name": "customer_site_code", "type": "string", "description": "Optional equality filter on customer_site_code.", "required": false, "sample_values": []}, {"name": "por_category", "type": "string", "description": "Optional equality filter on por_category.", "required": false, "sample_values": []}, {"name": "smp_name", "type": "string", "description": "Program filter. When \'NTM\', also enforces smp_status=\'Active\' and excludes specific ntm_project_type values.", "required": false, "sample_values": ["NTM", "AHLOB Modernization"]}, {"name": "smp_id", "type": "string", "description": "Optional equality filter on smp_id.", "required": false, "sample_values": []}, {"name": "phase", "type": "string", "description": "Which milestone to measure hold against. Default \'start\' uses anchor = pj_a_4225_construction_start_finish and delay-code = pj_construction_start_delay_code. \'complete\' uses anchor = pj_a_5175_construction_complete_finish and delay-code = pj_construction_complete_delay_code. Population is always restricted to projects whose chosen anchor IS NULL (still holding).", "required": false, "sample_values": ["start", "complete"]}, {"name": "start_date", "type": "date", "description": "Lower bound on pj_a_3925_msl_pickup_date_finish::date.", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Upper bound on pj_a_3925_msl_pickup_date_finish::date.", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": false}, {"name": "m_area", "type": "string", "nullable": false}, {"name": "m_market", "type": "string", "nullable": false}, {"name": "construction_gc", "type": "string", "nullable": false}, {"name": "delay_reason", "type": "string", "nullable": true}, {"name": "project_count", "type": "number", "nullable": false}, {"name": "avg_hold_days", "type": "number", "nullable": true}, {"name": "max_hold_days", "type": "number", "nullable": true}, {"name": "sla_breach_count", "type": "number", "nullable": false}, {"name": "sla_breach_pct", "type": "number", "nullable": true}], "sample_output": [], "row_count": 50}',
#     n.kpi_description = 'Material Hold Time by GC — measures how long each general contractor is CURRENTLY HOLDING material that has NOT YET reached the construction/swap milestone. This is a still-holding (open) snapshot, NOT a historical / closed-cycle metric.\n\nHARD CONSTRAINTS (the generated SQL MUST satisfy ALL three):\n  1. The chosen anchor column {cx_anchor_col} MUST be IS NULL (the milestone has not happened — material is still being held). Never write IS NOT NULL on the anchor.\n  2. hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date. Never subtract the anchor from pickup — the anchor is NULL by definition, so any subtraction with it would yield NULL.\n  3. Counts MUST use COUNT(DISTINCT pj_project_id). Never use s_site_id for counting.\n\nBy default the anchor is pj_a_4225_construction_start_finish (construction/swap start). When the user asks about hold until completion, the phase filter switches the anchor to pj_a_5175_construction_complete_finish (construction/swap complete). Reports project_count, avg_hold_days, max_hold_days, and 8-day SLA breach counts per region / area / market / GC / delay reason.',
#     n.kpi_formula_description = 'REQUIRED SQL TEMPLATE (the agent MUST generate SQL in exactly this shape — copy verbatim, substituting {cx_anchor_col}, {delay_code_col}, and optional filters):\n\nWITH base AS (\n    SELECT\n        "rgn_region", "m_area", "m_market", "construction_gc",\n        "{delay_code_col}" AS delay_reason,\n        "pj_project_id",\n        (CURRENT_DATE - "pj_a_3925_msl_pickup_date_finish"::date) AS hold_days\n        -- ↑ hold_days MUST use CURRENT_DATE (today) on the left; never use the anchor.\n    FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined\n    WHERE "pj_a_3925_msl_pickup_date_finish" IS NOT NULL\n      AND "{cx_anchor_col}" IS NULL\n      -- ↑ MUST: the chosen anchor column IS NULL (still holding — milestone has not happened).\n      -- Never write IS NOT NULL on {cx_anchor_col}.\n      -- <optional equality filters and date range go here, AND\'d in.>\n)\nSELECT\n    "rgn_region", "m_area", "m_market", "construction_gc", delay_reason,\n    COUNT(DISTINCT "pj_project_id") AS project_count,\n    -- ↑ Counts MUST use pj_project_id with DISTINCT; never s_site_id.\n    ROUND(AVG(hold_days)::numeric, 0) AS avg_hold_days,\n    MAX(hold_days) AS max_hold_days,\n    COUNT(DISTINCT CASE WHEN hold_days > 8 THEN "pj_project_id" END) AS sla_breach_count,\n    ROUND(100.0 * COUNT(DISTINCT CASE WHEN hold_days > 8 THEN "pj_project_id" END)\n          / NULLIF(COUNT(DISTINCT "pj_project_id"), 0), 2) AS sla_breach_pct\nFROM base\nGROUP BY "rgn_region", "m_area", "m_market", "construction_gc", delay_reason\nORDER BY avg_hold_days DESC NULLS LAST, "construction_gc", delay_reason;\n\nPLACEHOLDER SUBSTITUTIONS (the agent MUST resolve these literally — do not leave the\ncurly braces in the executed SQL):\n  phase=\'start\' (default):\n    {cx_anchor_col}  = pj_a_4225_construction_start_finish\n    {delay_code_col} = pj_construction_start_delay_code\n  phase=\'complete\':\n    {cx_anchor_col}  = pj_a_5175_construction_complete_finish\n    {delay_code_col} = pj_construction_complete_delay_code',
#     n.nl_description = 'Material Hold Time by GC — measures how long each general contractor is CURRENTLY HOLDING material that has NOT YET reached the construction/swap milestone. This is a still-holding (open) snapshot, NOT a historical / closed-cycle metric.\n\nHARD CONSTRAINTS (the generated SQL MUST satisfy ALL three):\n  1. The chosen anchor column {cx_anchor_col} MUST be IS NULL (the milestone has not happened — material is still being held). Never write IS NOT NULL on the anchor.\n  2. hold_days = CURRENT_DATE - pj_a_3925_msl_pickup_date_finish::date. Never subtract the anchor from pickup — the anchor is NULL by definition, so any subtraction with it would yield NULL.\n  3. Counts MUST use COUNT(DISTINCT pj_project_id). Never use s_site_id for counting.\n\nBy default the anchor is pj_a_4225_construction_start_finish (construction/swap start). When the user asks about hold until completion, the phase filter switches the anchor to pj_a_5175_construction_complete_finish (construction/swap complete). Reports project_count, avg_hold_days, max_hold_days, and 8-day SLA breach counts per region / area / market / GC / delay reason.'
# RETURN n.label AS label, n.node_id AS node_id;