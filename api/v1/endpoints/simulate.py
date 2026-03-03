"""
Simulate endpoint — POST /api/v1/simulate

Delegates to simulation_service; handles HTTP error mapping only.
"""
from fastapi import APIRouter, HTTPException

import services.simulation_service as sim_svc
from api.v1.schemas import SimulateRequest, SimulateResponse

router = APIRouter(tags=["Agent"])


@router.post("/simulate", response_model=SimulateResponse)
def simulate(req: SimulateRequest):
    """
    Run a natural-language query through the LangGraph agent pipeline.

    The agent:
    1. Discovers the KG schema
    2. Autonomously explores the graph using tools
    3. Synthesises a PM-ready response

    Returns the final response, structured data summary, and execution trace.
    """
    try:
        return sim_svc.run_query(req.query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
