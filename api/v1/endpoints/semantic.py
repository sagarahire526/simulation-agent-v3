"""
Semantic Retrieval endpoint — POST /api/v1/semantic/retrieve

Lets you manually test the semantic search pipeline:
  • Embeds the submitted question with text-embedding-3-small
  • Compares against all stored scenario embeddings using cosine similarity
  • Returns every row above the given threshold (default 70%), sorted by score

Useful for verifying that the right scenario guidance is being matched before
running a full simulation.
"""
from __future__ import annotations

import logging
import time

import psycopg2
from fastapi import APIRouter, HTTPException

import config
from api.v1.schemas import SemanticRetrieveRequest, SemanticRetrieveResponse, ScenarioMatch
from services.semantic_service import SemanticService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/semantic", tags=["Semantic Retrieval"])

# Lazy singleton so the DB connection is reused across requests
_semantic: SemanticService | None = None


def _get_semantic() -> SemanticService:
    global _semantic
    if _semantic is None:
        _semantic = SemanticService()
    return _semantic


def _count_total_indexed() -> int:
    """Return the total number of scenarios with embeddings in the DB."""
    try:
        conn = psycopg2.connect(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM pwc_semantic_information_schema.semantics_simulation "
            "WHERE embedding IS NOT NULL"
        )
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception:
        return -1  # DB unreachable — caller handles display


# ── Endpoint ───────────────────────────────────────────────────────────────

@router.post(
    "/retrieve",
    response_model=SemanticRetrieveResponse,
    summary="Test semantic retrieval",
)
def semantic_retrieve(body: SemanticRetrieveRequest):
    """
    Manually test the semantic retrieval pipeline.

    Submit any question and see which pre-defined scenarios match above the
    given **threshold** (default `0.70` = 70% cosine similarity).

    Each match includes:
    - `similarity_score` — raw cosine similarity (0–1)
    - `similarity_pct` — human-readable percentage
    - Full scenario row: `data_phase_questions`, `data_phase_steps`,
      `calculation_phase_steps`, `simulator_phase_steps`, `simulation_methodology`

    **threshold** must be between `0.0` and `1.0`.
    """
    if not (0.0 <= body.threshold <= 1.0):
        raise HTTPException(
            status_code=422,
            detail="threshold must be between 0.0 and 1.0",
        )

    t0 = time.perf_counter()

    try:
        svc = _get_semantic()
        raw_matches = svc.search_similar_scenarios(body.question, threshold=body.threshold)
    except Exception as e:
        logger.error("Semantic retrieval failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Retrieval error: {e}")

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    logger.info(
        "Semantic retrieve: %d match(es) for %.80s in %.0fms",
        len(raw_matches), body.question, elapsed,
    )

    total_indexed = _count_total_indexed()

    matches = [
        ScenarioMatch(
            scenario_id=m["scenario_id"],
            scenario=m["scenario"],
            data_phase_questions=m["data_phase_questions"],
            data_phase_steps=m["data_phase_steps"],
            calculation_phase_steps=m["calculation_phase_steps"],
            simulator_phase_steps=m["simulator_phase_steps"],
            simulation_methodology=m["simulation_methodology"],
            similarity_score=m["similarity_score"],
            similarity_pct=f"{m['similarity_score'] * 100:.1f}%",
        )
        for m in raw_matches
    ]

    return SemanticRetrieveResponse(
        question=body.question,
        threshold=body.threshold,
        total_scenarios_searched=total_indexed,
        matches_found=len(matches),
        matches=matches,
    )
