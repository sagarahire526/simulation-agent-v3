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
from fastapi import Depends
from fastapi.responses import FileResponse
from api.v1.router import router as v1_router
from api.v1.auth import require_bkg_admin
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


@app.get(
    "/bkg-admin",
    tags=["BKG Admin UI"],
    include_in_schema=False,
    dependencies=[Depends(require_bkg_admin)],
)
async def bkg_admin_ui():
    """Serve the single-page BKG admin interface (HTTP Basic protected)."""
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
# MATCH (n:BKGNode {node_id: '41587967-8a03-4503-908b-de7642c5c3ad'})
# SET n.kpi_python_function = 'def predict_scop_acceptance(pending_rows, cycle_tiers, group_by="construction_gc", overall_avg=None, holidays=None):\n    """SCOP Acceptance Predictor (pure transform, no SQL).\n\n    Each pending site is dated from the cycle time of ITS OWN group. cycle_tiers is an\n    ordered list of [dimension, {value: avg_business_days}] from the requested grouping\n    down through coarser fallback tiers (e.g. construction_gc then rgn_region). For a site\n    whose group has no cycle baseline in the window, the first coarser tier that has one is\n    used, and failing all tiers the overall_avg. Fully generic over the grouping dimension\n    and any filters (the orchestrator scopes every tier the same way).\n\n    predicted_acceptance_date = add_working_days(scop_submitted_date, round(avg)) advancing\n    Mon-Fri only (plus optional holidays). Output per pending site: pj_project_id, group_key,\n    scop_submitted_date, avg_cycle_business_days, cycle_source, predicted_acceptance_date.\n    """\n    from datetime import date, datetime, timedelta\n\n    def _to_date(v):\n        if v is None:\n            return None\n        if isinstance(v, datetime):\n            return v.date()\n        if isinstance(v, date):\n            return v\n        s = str(v).strip()\n        if not s:\n            return None\n        try:\n            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()\n        except Exception:\n            try:\n                return datetime.strptime(s[:10], "%Y-%m-%d").date()\n            except Exception:\n                return None\n\n    holiday_set = set()\n    for h in (holidays or []):\n        d = _to_date(h)\n        if d is not None:\n            holiday_set.add(d)\n\n    def add_working_days(start, n):\n        if start is None or n is None:\n            return None\n        n = int(n)\n        d = start\n        moved = 0\n        while moved < n:\n            d = d + timedelta(days=1)\n            if d.weekday() < 5 and d not in holiday_set:\n                moved += 1\n        return d\n\n    tiers = []\n    for entry in (cycle_tiers or []):\n        if isinstance(entry, (list, tuple)) and len(entry) == 2:\n            tiers.append((entry[0], entry[1] or {}))\n\n    out = []\n    for row in (pending_rows or []):\n        submitted = _to_date(row.get("scop_submitted_date"))\n        avg_cyc = None\n        source = None\n        for dim, amap in tiers:\n            val = row.get(dim)\n            if val is not None and val in amap and amap[val] is not None:\n                try:\n                    avg_cyc = float(amap[val])\n                    source = dim\n                    break\n                except (TypeError, ValueError):\n                    continue\n        if avg_cyc is None and overall_avg is not None:\n            try:\n                avg_cyc = float(overall_avg)\n                source = "overall"\n            except (TypeError, ValueError):\n                avg_cyc = None\n        predicted = add_working_days(submitted, round(avg_cyc)) if (submitted is not None and avg_cyc is not None) else None\n        out.append({\n            "pj_project_id":              row.get("pj_project_id"),\n            "group_key":                  row.get(group_by),\n            "scop_submitted_date":        submitted.isoformat() if submitted else None,\n            "avg_cycle_business_days":    (round(avg_cyc, 1) if avg_cyc is not None else None),\n            "cycle_source":               source,\n            "predicted_acceptance_date":  predicted.isoformat() if predicted else None,\n        })\n    return out\n';

# MATCH (n:BKGNode {node_id: 'scn-001-scop-acceptance-prediction'})
# SET n.scn_python_function = 'def run_scop_acceptance_prediction(run_node, run_transform, filter=None, group_by="construction_gc"):\n    """Deterministic orchestrator for SCOP Acceptance Date Prediction.\n\n    Each pending site is dated from the cycle time of ITS OWN group (generic over the\n    grouping dimension). When a group has no cycle baseline in the window, a coarser\n    geographic tier is used, and failing all tiers the overall average. Leads with a\n    summary, then the per-site predictions. No LLM.\n    """\n    filter = dict(filter or {})\n    gb = group_by or "construction_gc"\n\n    CYCLE_NID     = "109ef604-2e52-4082-8ebe-d4297e9daa52"\n    PENDING_NID   = "pending_scop_acceptance_sites"\n    PREDICTOR_NID = "41587967-8a03-4503-908b-de7642c5c3ad"\n\n    # coarse-to-coarser fallback chain per grouping dimension (overall is appended last\n    # by the predictor). Geographic dims roll up region to area to market to region.\n    fallback_chain = {\n        "m_market":        ["m_market", "m_area", "rgn_region"],\n        "m_area":          ["m_area", "rgn_region"],\n        "rgn_region":      ["rgn_region"],\n        "construction_gc": ["construction_gc", "rgn_region"],\n        "por_category":    ["por_category", "rgn_region"],\n        "smp_name":        ["smp_name"],\n    }\n    tiers = fallback_chain.get(gb, [gb])\n\n    # cycle time is FTR-baselined and windowed by filter.start_date/end_date\n    cyc_filter = dict(filter)\n    cyc_filter["ftr_only"] = True\n\n    cycle_tiers = []\n    primary_rows = []\n    for i in range(len(tiers)):\n        dim = tiers[i]\n        rows = run_node(CYCLE_NID, cyc_filter, dim) or []\n        if i == 0:\n            primary_rows = rows\n        amap = {}\n        for r in rows:\n            v = r.get("avg_business_days")\n            if v is not None:\n                amap[r.get(dim)] = v\n        cycle_tiers.append([dim, amap])\n\n    # overall fallback = site-weighted mean of the primary-grain cycle times\n    num = 0.0\n    den = 0.0\n    for r in primary_rows:\n        v = r.get("avg_business_days")\n        c = r.get("total_sites_accepted") or 0\n        if v is not None:\n            try:\n                num += float(v) * float(c)\n                den += float(c)\n            except (TypeError, ValueError):\n                pass\n    overall_avg = (num / den) if den > 0 else None\n    if overall_avg is None:\n        vals = [float(r.get("avg_business_days")) for r in primary_rows if r.get("avg_business_days") is not None]\n        overall_avg = (sum(vals) / len(vals)) if vals else None\n\n    # pending sites (drop the acceptance-date window, keep geo/program filters)\n    pend_filter = {k: v for k, v in filter.items() if k not in ("start_date", "end_date", "ftr_only")}\n    pending_rows = run_node(PENDING_NID, pend_filter, gb) or []\n\n    predictions = run_transform(PREDICTOR_NID, pending_rows, cycle_tiers,\n                                group_by=gb, overall_avg=overall_avg) or []\n\n    # ---- summarize the per-site predictions first, then the detail ----\n    by_group = {}\n    pdates = []\n    source_counts = {}\n    for p in predictions:\n        g = p.get("group_key") or "UNKNOWN"\n        b = by_group.setdefault(g, {"group": g, "sites": 0, "cycle_days_sum": 0.0,\n                                    "earliest_acceptance": None, "latest_acceptance": None})\n        b["sites"] += 1\n        try:\n            b["cycle_days_sum"] += float(p.get("avg_cycle_business_days") or 0)\n        except (TypeError, ValueError):\n            pass\n        pa = p.get("predicted_acceptance_date")\n        if pa:\n            pa = str(pa)\n            pdates.append(pa)\n            if b["earliest_acceptance"] is None or pa < b["earliest_acceptance"]:\n                b["earliest_acceptance"] = pa\n            if b["latest_acceptance"] is None or pa > b["latest_acceptance"]:\n                b["latest_acceptance"] = pa\n        src = p.get("cycle_source") or "unknown"\n        source_counts[src] = source_counts.get(src, 0) + 1\n\n    group_summary = []\n    for k in sorted(by_group):\n        b = by_group[k]\n        n = b["sites"] or 1\n        group_summary.append({\n            "group": b["group"], "sites": b["sites"],\n            "avg_cycle_business_days": round(b["cycle_days_sum"] / n, 1),\n            "earliest_acceptance": b["earliest_acceptance"],\n            "latest_acceptance": b["latest_acceptance"],\n        })\n\n    summary = {\n        "pending_sites": len(pending_rows),\n        "predicted_sites": len(predictions),\n        "earliest_predicted_acceptance": min(pdates) if pdates else None,\n        "latest_predicted_acceptance": max(pdates) if pdates else None,\n        "cycle_source_breakdown": source_counts,\n        "by_group": group_summary,\n    }\n\n    return {\n        "scenario":       "scop_acceptance_prediction",\n        "group_by":       gb,\n        "filter":         filter,\n        "summary":        summary,\n        "cycle_baseline": primary_rows,\n        "pending_count":  len(pending_rows),\n        "predictions":    predictions,\n    }\n';
