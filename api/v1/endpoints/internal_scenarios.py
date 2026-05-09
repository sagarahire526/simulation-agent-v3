"""
Internal Scenario Library endpoints — CRUD over the local JSON-backed simulation
scenario store. Used as a stopgap until these scenarios are ingested into GCL.

  POST   /api/v1/internal-scenarios            → add a new scenario (embeds the question)
  GET    /api/v1/internal-scenarios            → list all (no embeddings)
  GET    /api/v1/internal-scenarios/search     → semantic search by query
  DELETE /api/v1/internal-scenarios/{scenario_id}
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services import internal_scenarios as scenario_lib

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal-scenarios", tags=["Internal Scenario Library"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class ScenarioIn(BaseModel):
    tag: str = Field(..., description="Short tag, e.g. 'Crew shortage % impact in a region'.")
    question: str = Field(..., description="The user-style question this scenario answers.")
    steps: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered list of solve steps from the program-office workflow. "
            "Mixed retrieval + synthesis — the planner filters at runtime."
        ),
    )


class ScenarioOut(BaseModel):
    id: str
    tag: str
    question: str
    steps: list[str]
    embedding_model: str | None = None
    created_at: str | None = None


class SearchHit(BaseModel):
    id: str | None
    tag: str | None
    question: str | None
    steps: list[str]
    similarity_score: float


class AddResponse(BaseModel):
    status: str
    scenario: ScenarioOut


class DeleteResponse(BaseModel):
    status: str
    deleted_id: str


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("", response_model=AddResponse, status_code=201)
def add_scenario(payload: ScenarioIn) -> AddResponse:
    """
    Embed the scenario's question and append it to the library.
    Returns the saved entry (without the embedding vector).
    """
    try:
        entry = scenario_lib.add(payload.tag, payload.question, payload.steps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to add internal scenario")
        raise HTTPException(status_code=500, detail=f"Failed to add scenario: {exc}")
    return AddResponse(status="ok", scenario=ScenarioOut(**entry))


@router.get("", response_model=list[ScenarioOut])
def list_scenarios() -> list[ScenarioOut]:
    """List all stored scenarios. Embedding vectors are excluded from the response."""
    entries = scenario_lib.list_all(include_embeddings=False)
    return [ScenarioOut(**e) for e in entries]


@router.get("/search", response_model=list[SearchHit])
def search_scenarios(
    query: str = Query(..., description="Natural-language query to match against scenarios."),
    top_k: int = Query(1, ge=1, le=10, description="Max matches to return."),
    min_similarity: float = Query(
        scenario_lib.MIN_SIMILARITY, ge=0.0, le=1.0,
        description="Cosine-similarity floor; matches below this are dropped.",
    ),
) -> list[SearchHit]:
    """
    Semantic search over stored scenarios. Useful for testing the threshold
    without spinning up the full planner pipeline.
    """
    matches = scenario_lib.search(query, top_k=top_k, min_similarity=min_similarity)
    return [SearchHit(**m) for m in matches]


@router.delete("/{scenario_id}", response_model=DeleteResponse)
def delete_scenario(scenario_id: str) -> DeleteResponse:
    """Remove a scenario by id."""
    removed = scenario_lib.delete(scenario_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found.")
    return DeleteResponse(status="ok", deleted_id=scenario_id)
