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


# MATCH (n:BKGNode { session_id: '69a3d22f26e208edc083a06e', node_id: 'a9310885-be13-4778-bc99-7ead870d99ee' })
# SET
#     n.kpi_python_function = 'def get_cx_to_on_air_backlog(execute_query, filters=None) -> list[dict]:\n    """CX to On-Air Backlog (evaluation-date snapshot).\n\n    A project is in backlog when:\n      - "pj_a_5175_construction_complete_finish" IS NOT NULL (construction complete)\n      - AND "por_first_completion_objective_actual" IS NULL (not yet on-air)\n\n    Aging (days_in_backlog) is computed per project as:\n      eval_date - "pj_a_5175_construction_complete_finish"::date\n\n    Output:\n      rgn_region, m_area, m_market, construction_gc,\n      total_cx_complete_not_on_air,\n      avg_days_in_backlog, p50_days_in_backlog, p90_days_in_backlog, max_days_in_backlog\n\n    Note: Legacy pending_* columns were removed because under the simplified Nokia definition\n    they became duplicates of the main backlog count.\n    """\n\n    filters = filters or {}\n\n    def _sql_str(v):\n        return str(v).replace("\'", "\'\'")\n\n    # Allowlisted equality filters (no generic filter loops)\n    allowed_filters = {\n        "rgn_region": "rgn_region",\n        "m_area": "m_area",\n        "m_market": "m_market",\n        "construction_gc": "construction_gc",\n        "pj_project_id": "pj_project_id",\n        "s_site_id": "s_site_id",\n        "smp_name": "smp_name",\n    }\n\n    # evaluation date parameter handling (Weekly GC Run Rate-style aliases)\n    evaluation_date = (\n        filters.get("evaluation_date")\n        or filters.get("as_of_date")\n        or filters.get("date")\n    )\n    if evaluation_date:\n        eval_date_sql = f"DATE \'{_sql_str(evaluation_date)}\'"\n    else:\n        eval_date_sql = "CURRENT_DATE"\n\n    # Optional date range filters apply to the construction complete date\n    start_date = filters.get("start_date")\n    end_date = filters.get("end_date")\n\n    # Seed WHERE with 1=1, then add defining metric predicates first\n    where_parts = [\n        "1=1",\n        # Backlog inclusion logic (liberal complete, strict not-complete)\n        "\\"pj_a_5175_construction_complete_finish\\"::date IS NOT NULL",\n        "\\"por_first_completion_objective_actual\\"::date IS NULL",\n    ]\n\n    # Date range filters (construction completion date window)\n    if start_date:\n        sd = _sql_str(start_date)\n        where_parts.append(\n            f"\\"pj_a_5175_construction_complete_finish\\"::date >= DATE \'{sd}\'"\n        )\n    if end_date:\n        ed = _sql_str(end_date)\n        where_parts.append(\n            f"\\"pj_a_5175_construction_complete_finish\\"::date <= DATE \'{ed}\'"\n        )\n\n    # Equality filters\n    for k, col in allowed_filters.items():\n        if k in filters and filters[k] is not None and filters[k] != "":\n            where_parts.append(f"\\"{col}\\" = \'{_sql_str(filters[k])}\'")\n\n    # NTM-specific filters (per Nokia Question Bank)\n    if filters.get(\'smp_name\') and str(filters[\'smp_name\']).strip().upper() == \'NTM\':\n        where_parts.append(\'"smp_status" = \\\'Active\\\'\')\n        where_parts.append(\'"ntm_project_type" NOT IN (\\\'E2E06 - NSD Cx Mgmt\\\', \\\'E2E07 - OL/MOD Cx Mgmt\\\')\')\n\n\n    where_sql = "WHERE " + " AND ".join(where_parts)\n\n    sql = f"""\n    SELECT\n      base.\\"rgn_region\\" AS rgn_region,\n      base.\\"m_area\\" AS m_area,\n      base.\\"m_market\\" AS m_market,\n      base.\\"construction_gc\\" AS construction_gc,\n\n      COUNT(DISTINCT base.\\"pj_project_id\\") AS total_cx_complete_not_on_air,\n\n      AVG(base.days_in_backlog)::float AS avg_days_in_backlog,\n      MAX(base.days_in_backlog) AS max_days_in_backlog\n\n    FROM (\n      SELECT\n        \\"rgn_region\\",\n        \\"m_area\\",\n        \\"m_market\\",\n        \\"construction_gc\\",\n        \\"pj_project_id\\",\n        (\n          {eval_date_sql}\n          - \\"pj_a_5175_construction_complete_finish\\"::date\n        ) AS days_in_backlog\n      FROM pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined\n      {where_sql}\n    ) base\n    WHERE base.days_in_backlog >= 0\n    GROUP BY base.\\"rgn_region\\", base.\\"m_area\\", base.\\"m_market\\", base.\\"construction_gc\\"\n    ORDER BY base.\\"rgn_region\\", base.\\"m_area\\", base.\\"m_market\\", base.\\"construction_gc\\"\n    """\n\n    return execute_query(sql)\n',
#     n.kpi_source_columns = ['rgn_region', 'm_area', 'm_market', 'construction_gc', 'pj_project_id', 's_site_id', 'pj_a_5175_construction_complete_finish', 'smp_name', 'smp_status', 'ntm_project_type', 'por_first_completion_objective_actual'],
#     n.kpi_business_logic = 'Determine eval_date as CURRENT_DATE unless filters.evaluation_date / filters.as_of_date / filters.date is provided; if provided use DATE \'YYYY-MM-DD\'.\nDefine construction_complete_date = "pj_a_5175_construction_complete_finish"::date.\nInclude a project in the backlog base set if ("pj_a_5175_construction_complete_finish"::date IS NOT NULL) AND ("por_first_completion_objective_actual"::date IS NULL).\nIf start_date provided, require construction_complete_date >= start_date. If end_date provided, require construction_complete_date <= end_date.\nApply allowlisted equality filters (rgn_region, m_area, m_market, construction_gc, pj_project_id, s_site_id) when provided.\nCompute days_in_backlog = eval_date - construction_complete_date; exclude rows where days_in_backlog < 0.\nGroup by (rgn_region, m_area, m_market, construction_gc) and compute: COUNT(DISTINCT pj_project_id) as total_cx_complete_not_on_air; AVG(days_in_backlog), and MAX(days_in_backlog).',
#     n.kpi_formula_description = 'CX to On-Air Backlog (as-of evaluation_date, default CURRENT_DATE): per (rgn_region, m_area, m_market, construction_gc) count DISTINCT pj_project_id where construction is complete in EITHER system ("pj_a_5175_construction_complete_finish"::date IS NOT NULL) AND integration is NOT complete in EITHER system ("por_first_completion_objective_actual"::date IS NULL). Aging is computed per project as days_in_backlog = evaluation_date - "pj_a_5175_construction_complete_finish"::date; rows with days_in_backlog < 0 are excluded. Output includes total_cx_complete_not_on_air plus aging aggregates avg_days_in_backlog, max_days_in_backlog. Legacy pending_* columns were removed because they were duplicates of the main count under the simplified definition.',
#     n.nl_description = 'CX to On-Air Backlog (as-of evaluation_date, default CURRENT_DATE): per (rgn_region, m_area, m_market, construction_gc) count DISTINCT pj_project_id where construction is complete in EITHER system ("pj_a_5175_construction_complete_finish"::date IS NOT NULL) AND integration is NOT complete in EITHER system ("por_first_completion_objective_actual"::date IS NULL). Aging is computed per project as days_in_backlog = evaluation_date - "pj_a_5175_construction_complete_finish"::date; rows with days_in_backlog < 0 are excluded. Output includes total_cx_complete_not_on_air plus aging aggregates avg_days_in_backlog, max_days_in_backlog. Legacy pending_* columns were removed because they were duplicates of the main count under the simplified definition. Optionally filterable by program via smp_name (e.g., \'NTM\' for Macro, \'AHLOB Modernization\' for AHLOB).',
#     n.definition = 'CX to On-Air Backlog (as-of evaluation_date, default CURRENT_DATE): per (rgn_region, m_area, m_market, construction_gc) count DISTINCT pj_project_id where construction is complete in EITHER system ("pj_a_5175_construction_complete_finish"::date IS NOT NULL) AND integration is NOT complete in EITHER system ("por_first_completion_objective_actual"::date IS NULL). Aging is computed per project as days_in_backlog = evaluation_date - "pj_a_5175_construction_complete_finish"::date; rows with days_in_backlog < 0 are excluded. Output includes total_cx_complete_not_on_air plus aging aggregates avg_days_in_backlog, max_days_in_backlog. Legacy pending_* columns were removed because they were duplicates of the main count under the simplified definition.',
#     n.kpi_description = 'Counts projects where construction is complete but the site is not yet On-Air as of a current-state (CURRENT_DATE) snapshot. The KPI also breaks this backlog into blocker-stage buckets to show where projects are stuck in the process. Optionally filterable by program via smp_name (e.g., \'NTM\' for Macro, \'AHLOB Modernization\' for AHLOB).',
#     n.kpi_contract = '{"function_name": "get_cx_to_on_air_backlog", "node_type": "kpi", "node_id": "a9310885-be13-4778-bc99-7ead870d99ee", "node_label": "CX to On-Air Backlog", "description": "Counts projects where construction is complete but the site is not yet On-Air as of a current-state (CURRENT_DATE) snapshot. The KPI also breaks this backlog into blocker-stage buckets to show where projects are stuck in the process.", "parameters": [{"name": "evaluation_date", "type": "date", "description": "Filter by evaluation date", "required": false, "sample_values": []}, {"name": "as_of_date", "type": "date", "description": "Filter by as of date", "required": false, "sample_values": []}, {"name": "date", "type": "date", "description": "Filter by date", "required": false, "sample_values": []}, {"name": "start_date", "type": "date", "description": "Filter by start date", "required": false, "sample_values": []}, {"name": "end_date", "type": "date", "description": "Filter by end date", "required": false, "sample_values": []}], "output_columns": [{"name": "rgn_region", "type": "string", "nullable": false}, {"name": "m_area", "type": "string", "nullable": false}, {"name": "m_market", "type": "string", "nullable": false}, {"name": "construction_gc", "type": "string", "nullable": false}, {"name": "total_cx_complete_not_on_air", "type": "number", "nullable": false}, {"name": "avg_days_in_backlog", "type": "number", "nullable": false}, {"name": "max_days_in_backlog", "type": "number", "nullable": false}], "sample_output": [], "row_count": 10}'
# RETURN n.label AS label, n.node_id AS node_id;