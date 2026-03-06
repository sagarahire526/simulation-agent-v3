"""
Pydantic request / response schemas for the v1 API.

All models live here so endpoints stay thin and types are reusable.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


# ── Simulate ──────────────────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    user_id: str                       # Supplied via Swagger for now; passed by frontend later
    query: str
    thread_id: Optional[str] = None  # Caller-supplied conversation ID for HITL

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user-001",
                "query": "How many active GC sites are in Chicago?",
                "thread_id": "session-abc-123",
            }
        }
    }


class ClarificationPayload(BaseModel):
    """Payload returned when the query refiner pauses for user input."""
    type: str
    original_query: str
    questions: list[str]
    assumptions_if_skipped: list[str]
    message: str


class SimulateResponse(BaseModel):
    status: str                        # "complete" | "clarification_needed"
    thread_id: str                     # Echo back so caller can resume
    final_response: str
    data_summary: dict[str, Any]
    calculations: str
    errors: list[str]
    messages: list[dict[str, Any]]
    traversal_steps: int
    routing_decision: str              # "greeting" | "traversal" | "simulation"
    planning_rationale: str            # Business-intent rationale for the plan (simulation route)
    planner_steps: list[str]
    clarification: Optional[ClarificationPayload] = None


# ── Resume (HITL) ─────────────────────────────────────────────────────────────

class ResumeRequest(BaseModel):
    thread_id: str
    clarification: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "thread_id": "session-abc-123",
                "clarification": "Chicago market, target is 300 sites by end of next week.",
            }
        }
    }


# ── BKG ───────────────────────────────────────────────────────────────────────

class BKGQueryRequest(BaseModel):
    mode: str
    node_id: Optional[str] = None
    metric_id: Optional[str] = None
    question: Optional[str] = None
    start: Optional[str] = None
    depth: Optional[int] = 2
    rel_type: Optional[str] = None
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
    similarity_pct: str


class SemanticRetrieveResponse(BaseModel):
    question: str
    threshold: float
    total_scenarios_searched: int
    matches_found: int
    matches: list[ScenarioMatch]


# ── Threads ───────────────────────────────────────────────────────────────────

class ThreadSummary(BaseModel):
    thread_id: str
    user_id: str
    created_at: Any
    last_active_at: Any
    status: str
    total_queries: int


class MessageRecord(BaseModel):
    query_id: str
    thread_id: str
    user_id: str
    original_query: str
    refined_query: Optional[str] = None
    routing_decision: Optional[str] = None
    planning_rationale: Optional[Any] = None   # JSON array of planner steps
    final_response: Optional[str] = None
    started_at: Any
    completed_at: Optional[Any] = None
    duration_ms: Optional[float] = None
    status: str


class ClarificationStatus(BaseModel):
    is_paused: bool
    clarification_id: Optional[str] = None
    query_id: Optional[str] = None
    questions_asked: Optional[list[str]] = None
    assumptions_offered: Optional[list[str]] = None
    asked_at: Optional[Any] = None


# ── Sandbox ───────────────────────────────────────────────────────────────────

class SandboxRequest(BaseModel):
    code: str
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
