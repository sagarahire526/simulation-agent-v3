"""
BKG endpoints
  GET  /api/v1/schema
  POST /api/v1/bkg/query

Delegates to bkg_service; handles HTTP error mapping only.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException

import services.bkg_service as bkg_svc
from api.v1.schemas import BKGQueryRequest

router = APIRouter(tags=["BKG"])


@router.get("/schema")
def get_schema(table_name: Optional[str] = None):
    """
    Return the BKG schema overview.

    Pass `table_name` to get BKGNodes mapped to a specific table,
    or omit it for a full overview of all tables.
    """
    try:
        return bkg_svc.get_schema(table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bkg/query")
def bkg_query(req: BKGQueryRequest):
    """
    Query the Business Knowledge Graph directly.

    **Modes:**

    | mode | required fields | description |
    |------|----------------|-------------|
    | `get_node` | `node_id` | Fetch a BKGNode by ID with all properties and relationships |
    | `find_relevant` | `question` | Keyword search across all BKGNodes |
    | `traverse` | `start`, `depth` | Walk relationships from a start node |
    | `get_kpi` | `node_id` | Get KPI computation details (formula, logic, function) |
    | `schema` | *(none)* | List all tables / mapped BKGNodes |
    """
    try:
        return bkg_svc.query(req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
