"""
Pydantic request / response schemas for the v1 API.

All models live here so endpoints stay thin and types are reusable.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


# ── Project Type Enum ─────────────────────────────────────────────────────────

class ProjectType(str, Enum):
    NTM = "NTM"
    AHLOB = "AHLOB Modernization"
    BOTH = "NTM,AHLOB Modernization"
    NAS = "NAS"


# ── Simulate ──────────────────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    user_id: str                       # Supplied via Swagger for now; passed by frontend later
    query: str
    project_type: ProjectType          # Dropdown: NTM, AHLOB Modernization, Both, or NAS
    thread_id: Optional[str] = None    # Caller-supplied conversation ID for HITL

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user-001",
                "query": "How many active GC sites are in Chicago?",
                "project_type": "NTM",  # Options: "NTM", "AHLOB Modernization", "NTM,AHLOB Modernization", "NAS"
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
    final_response: str
    current_status: list[str] = []     # Flattened rows from the markdown's "## Current Status" table (sibling to final_response)
    execution_algorithm: str = ""      # Numbered step-by-step narrative of how the system answered
    thread_id: str
    errors: list[str]
    routing_decision: str              # "greeting" | "traversal" | "simulation"
    planner_steps: list[str]
    graph: Optional[dict[str, Any]] = None  # Highcharts-compatible chart JSON
    analysis: Optional[dict[str, list]] = None  # Semantic search headings: keywords, kpis, questions, scenarios
    traces: Optional[dict[str, Any]] = None  # Execution trace: nodes, tool calls, results
    clarification: Optional[ClarificationPayload] = None  # Present when status="clarification_needed"


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
    question: Optional[str] = None
    start: Optional[str] = None
    depth: Optional[int] = 2
    rel_type: Optional[str] = None
    table_name: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"mode": "get_node",      "node_id": "site"},
                {"mode": "find_relevant", "question": "contractor project site"},
                {"mode": "traverse",      "start": "site", "depth": 2},
                {"mode": "get_kpi",       "node_id": "on_air_cycle_time"},
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

class CreateThreadRequest(BaseModel):
    user_id: str
    thread_name: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user-001",
                "thread_name": "Chicago sites analysis",
            }
        }
    }


class ThreadSummary(BaseModel):
    thread_id: str
    user_id: str
    thread_name: Optional[str] = None
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
    current_status: list[str] = []             # Flattened rows from the markdown's "## Current Status" table (sibling to final_response)
    algorithm: Optional[str] = None            # Step-by-step execution narrative
    graph: Optional[dict[str, Any]] = None     # Highcharts-compatible chart JSON
    analysis: Optional[dict[str, list]] = None  # Semantic search headings
    traces: Optional[dict[str, Any]] = None    # Execution trace: nodes, tool calls, results
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


# ── Feedback ──────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    thread_id: str
    query_id: str                       # References the specific chat turn being rated
    user_id: str
    username: str
    rating: Optional[int] = None        # Numeric rating (1-5)
    is_positive: Optional[bool] = None  # Thumbs up (true) / thumbs down (false)
    comment: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "thread_id": "session-abc-123",
                "query_id": "query-xyz-456",
                "user_id": "user-001",
                "username": "sagar.ahire",
                "rating": 4,
                "is_positive": True,
                "comment": "Very accurate simulation results!",
            }
        }
    }


class FeedbackOut(BaseModel):
    feedback_id: str
    thread_id: str
    query_id: str
    user_id: str
    username: str
    rating: Optional[int] = None
    is_positive: Optional[bool] = None
    comment: Optional[str] = None
    created_at: Any


class FeedbackSubmitResponse(BaseModel):
    feedback_id: str
    status: str = "submitted"


class FeedbackStats(BaseModel):
    total: int
    avg_rating: Optional[float] = None
    thumbs_up: int
    thumbs_down: int


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
