"""
BKG Admin endpoints — power the /bkg-admin HTML interface.

All routes require a `graph_key` (currently "ntm_ahlob" or "nas") which is
resolved server-side to a Neo4j `session_id` so the UI never needs to know
the underlying IDs.

  GET    /api/v1/bkg-admin/graphs
  GET    /api/v1/bkg-admin/nodes?graph_key=...&entity_type=...
  GET    /api/v1/bkg-admin/nodes/{node_id}?graph_key=...
  PUT    /api/v1/bkg-admin/nodes/{node_id}?graph_key=...
  POST   /api/v1/bkg-admin/nodes?graph_key=...
  POST   /api/v1/bkg-admin/relationships?graph_key=...
  DELETE /api/v1/bkg-admin/relationships/{edge_id}?graph_key=...
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import services.bkg_admin_service as svc

router = APIRouter(prefix="/bkg-admin", tags=["BKG Admin"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class NodeUpdate(BaseModel):
    """Whitelisted business-field updates. Non-whitelisted keys are ignored."""

    updates: dict[str, Any] = Field(default_factory=dict)


class RelationshipSpec(BaseModel):
    direction: str = Field(default="out", description="'out' or 'in'")
    target_node_id: str
    relationship_type: str
    relationship: Optional[str] = None


class NodeCreate(BaseModel):
    node_id: str
    entity_type: str = Field(description="core | context | transaction | reference | kpi")
    properties: dict[str, Any] = Field(default_factory=dict)
    relationships: list[RelationshipSpec] = Field(default_factory=list)


class RelationshipCreate(BaseModel):
    source_node_id: str
    target_node_id: str
    relationship_type: str
    relationship: Optional[str] = None


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/graphs")
def list_graphs():
    """Return the static set of editable graphs for the dropdown."""
    return {"graphs": svc.list_graphs()}


@router.get("/nodes")
def list_nodes(
    graph_key: str = Query(...),
    entity_type: Optional[str] = Query(None),
):
    try:
        return {"nodes": svc.list_nodes(graph_key, entity_type)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/nodes/{node_id}")
def get_node(node_id: str, graph_key: str = Query(...)):
    try:
        return svc.get_node(graph_key, node_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/nodes/{node_id}")
def update_node(node_id: str, body: NodeUpdate, graph_key: str = Query(...)):
    try:
        return svc.update_node(graph_key, node_id, body.updates)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/nodes")
def create_node(body: NodeCreate, graph_key: str = Query(...)):
    try:
        return svc.create_node(
            graph_key=graph_key,
            node_id=body.node_id,
            entity_type=body.entity_type,
            properties=body.properties,
            relationships=[r.model_dump() for r in body.relationships],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str, graph_key: str = Query(...)):
    try:
        return svc.delete_node(graph_key, node_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/relationships")
def add_relationship(body: RelationshipCreate, graph_key: str = Query(...)):
    try:
        return svc.add_relationship(
            graph_key=graph_key,
            source_node_id=body.source_node_id,
            target_node_id=body.target_node_id,
            relationship_type=body.relationship_type,
            relationship=body.relationship,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/relationships/{edge_id}")
def delete_relationship(edge_id: str, graph_key: str = Query(...)):
    try:
        ok = svc.delete_relationship(graph_key, edge_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Edge '{edge_id}' not found")
        return {"deleted": True, "edge_id": edge_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
