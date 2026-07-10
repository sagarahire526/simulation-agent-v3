"""
Node Runner endpoint — POST /api/v1/bkg-nodes/execute

Executes a single BKG node's stored python function in ISOLATION against the
read-only Postgres, using the SAME sandbox helpers the agent uses
(run_node / run_scenario / run_transform). Lets you verify a node's logic + data
without driving the whole agent flow. The generated SQL is emitted to the server
log via the sandbox's own `execute_query` logging.

This runs ONLY stored functions selected by node_id — it is NOT an arbitrary-code
surface (that is /sandbox/execute). Delegates to sandbox_service.
"""
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import services.sandbox_service as sandbox_svc

router = APIRouter(tags=["BKG Node Runner"])


class NodeRunRequest(BaseModel):
    mode: str = Field(
        default="node",
        description="'node' (kpi/core get_*), 'scenario' (scn orchestrator), or "
                    "'transform' (pure predictor).",
    )
    node_id: str = Field(description="BKGNode node_id to execute.")
    # node / scenario
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Filter dict passed to the function (rgn_region, smp_name, "
                    "start_date, end_date, ftr_only, …).",
    )
    group_by: Optional[str] = Field(default=None, description="group_by dimension (node/scenario).")
    # transform
    args: list[Any] = Field(default_factory=list, description="Positional args (transform mode).")
    kwargs: dict[str, Any] = Field(default_factory=dict, description="Keyword args (transform mode).")
    timeout_seconds: int = Field(default=120, ge=1, le=600)


@router.post("/bkg-nodes/execute")
def execute_bkg_node(req: NodeRunRequest):
    """
    Run a stored node function and return the sandbox result.

    **Response** mirrors the sandbox: `{status, result, output, error}`. For `node`
    mode `result` is a list of rows; for `scenario` it is the orchestrator's dict
    (cycle_baseline / pending_count / predictions); for `transform` it is whatever
    the transform returns.

    **Example (scenario):**
    ```json
    {
      "mode": "scenario",
      "node_id": "scn-001-scop-acceptance-prediction",
      "filters": {"rgn_region": "SOUTH", "smp_name": "NTM",
                  "start_date": "2026-01-07", "end_date": "2026-07-07"},
      "group_by": "m_market"
    }
    ```

    Reflects whatever cypher is currently loaded in Neo4j (functions are fetched
    from BKGNode by node_id).
    """
    try:
        return sandbox_svc.run_bkg_node(
            req.mode,
            req.node_id,
            filters=req.filters,
            group_by=req.group_by,
            args=req.args,
            kwargs=req.kwargs,
            timeout_seconds=req.timeout_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
