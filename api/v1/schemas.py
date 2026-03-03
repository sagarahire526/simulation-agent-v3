"""
Pydantic request / response schemas for the v1 API.

All models live here so endpoints stay thin and types are reusable.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


# ── Simulate ──────────────────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    query: str

    model_config = {
        "json_schema_extra": {
            "example": {"query": "How many active GC sites are in Chicago?"}
        }
    }


class SimulateResponse(BaseModel):
    final_response: str
    data_summary:   dict[str, Any]
    calculations:   str
    errors:         list[str]
    messages:       list[dict[str, Any]]
    traversal_steps: int


# ── BKG ───────────────────────────────────────────────────────────────────────

class BKGQueryRequest(BaseModel):
    mode:       str
    node_id:    Optional[str] = None
    metric_id:  Optional[str] = None
    question:   Optional[str] = None
    start:      Optional[str] = None
    depth:      Optional[int] = 2
    rel_type:   Optional[str] = None
    table_name: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"mode": "get_node",      "node_id": "GeneralContractor"},
                {"mode": "find_relevant", "question": "contractor project site"},
                {"mode": "traverse",      "start": "GeneralContractor", "depth": 2},
                {"mode": "diagnostic",    "metric_id": "completion_rate"},
                {"mode": "schema"},
            ]
        }
    }


# ── Semantic Retrieval ────────────────────────────────────────────────────────

class SemanticRetrieveRequest(BaseModel):
    question: str
    threshold: float = 0.70

    model_config = {
        "json_schema_extra": {
            "example": {
                "question": "Share me the weekly plan for Chicago market to complete 100 sites",
                "threshold": 0.70,
            }
        }
    }


class ScenarioMatch(BaseModel):
    scenario_id: int
    scenario: str
    data_phase_questions: list[str]
    data_phase_steps: list[str]
    calculation_phase_steps: list[str]
    simulator_phase_steps: list[str]
    simulation_methodology: str
    similarity_score: float
    similarity_pct: str  # e.g. "76.4%"


class SemanticRetrieveResponse(BaseModel):
    question: str
    threshold: float
    total_scenarios_searched: int
    matches_found: int
    matches: list[ScenarioMatch]


# ── Sandbox ───────────────────────────────────────────────────────────────────

class SandboxRequest(BaseModel):
    code:            str
    timeout_seconds: int = 30

    model_config = {
        "json_schema_extra": {
            "example": {
                "code": (
                    "df = pd.read_sql('SELECT 1 AS test', conn)\n"
                    "result = {'data': df.to_dict(orient='records')}"
                ),
                "timeout_seconds": 30,
            }
        }
    }
