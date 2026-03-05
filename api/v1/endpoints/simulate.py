"""
Simulate endpoints.

  POST /api/v1/simulate        — Start a new simulation (may pause for clarification)
  POST /api/v1/simulate/resume — Resume a paused simulation with user clarification
"""
import uuid

from fastapi import APIRouter, HTTPException

import services.simulation_service as sim_svc
from api.v1.schemas import SimulateRequest, SimulateResponse, ResumeRequest

router = APIRouter(tags=["Agent"])


@router.post("/simulate", response_model=SimulateResponse)
def simulate(req: SimulateRequest):
    """
    Run a natural-language query through the LangGraph agent pipeline.

    Pipeline:
      1. Query Refiner — validates completeness; may pause for clarification.
      2. Orchestrator  — routes to the right downstream pipeline.
      3a. (greeting)   → response directly.
      3b. (traversal)  → schema discovery → traversal → response.
      3c. (simulation) → schema discovery → planner (parallel steps) → response.

    If the query is under-specified, the response will have
    ``status="clarification_needed"`` and a ``clarification`` payload.
    Supply the returned ``thread_id`` to ``POST /simulate/resume`` to continue.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    try:
        result = sim_svc.run_query(req.query, thread_id=thread_id)
        return SimulateResponse(thread_id=thread_id, **result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/simulate/resume", response_model=SimulateResponse)
def simulate_resume(req: ResumeRequest):
    """
    Resume a simulation that paused for user clarification.

    Supply the ``thread_id`` from the original ``/simulate`` response
    and the user's clarification text.
    """
    try:
        result = sim_svc.resume_query(req.clarification, thread_id=req.thread_id)
        return SimulateResponse(thread_id=req.thread_id, **result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
