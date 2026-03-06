# Simulation Agent — LangGraph Multi-Agent System

A LangGraph-based multi-agent system that simulates telecom tower deployment scenarios for Project Managers. It traverses a Neo4j Business Knowledge Graph (BKG) and PostgreSQL database to answer planning, feasibility, and scheduling queries.

---

## Architecture

```
User Query
    │
    ▼
┌──────────────────────┐
│   Query Refiner      │  ← Validates scope; may pause (HITL) for
│   [HITL Node]        │    clarification (market, timeframe, target)
└──────────┬───────────┘
           │  (refined query)
           ▼
┌──────────────────────┐
│    Orchestrator      │  ← Classifies and routes
└──────────┬───────────┘
      ┌────┴─────────────────────┐
      │                          │
      ▼                          ▼
  "greeting"            "traversal" | "simulation"
  (respond directly)             │
                                 ▼
                      ┌──────────────────────┐
                      │   Schema Discovery   │  ← Fetches Neo4j KG schema
                      └──────────┬───────────┘
                            ┌────┴────────────┐
                            │                 │
                            ▼                 ▼
                   "traversal" path   "simulation" path
                            │                 │
                            ▼                 ▼
                   ┌────────────────┐  ┌──────────────────┐
                   │ Traversal Agent│  │  Planner Agent   │
                   │  (single run)  │  │  (decomposes →   │
                   └───────┬────────┘  │  N parallel steps)│
                           │           └────────┬──────────┘
                           │                    │ N parallel
                           │           ┌────────▼──────────┐
                           │           │ Traversal Agent×N  │
                           │           │ (ThreadPoolExecutor)│
                           │           └────────┬──────────┘
                           └──────────┬─────────┘
                                      ▼
                           ┌──────────────────────┐
                           │   Response Agent     │  ← Synthesises findings,
                           └──────────┬───────────┘    runs calculations,
                                      ▼                generates PM report
                              Simulation Result
```

---

## Agents

| Agent | Role | Key Behaviour |
|---|---|---|
| **Query Refiner** | HITL scope checker | Validates geography, timeframe, and target volume. Pauses via `interrupt()` if missing. |
| **Orchestrator** | Router | Classifies as `greeting`, `traversal`, or `simulation`. |
| **Schema Discovery** | KG context provider | Fetches Neo4j node labels, relationships, properties — injected into Planner and Traversal. |
| **Planner** | Sub-query decomposer | Breaks complex queries into 2–9 independent sub-queries; runs them in parallel. |
| **Traversal** | Data gatherer | Autonomous ReAct agent — uses 8 tools to query Neo4j and PostgreSQL, run calculations. |
| **Response** | Report generator | Synthesises all findings, runs Python calculations, outputs a structured PM report with 3 scenarios. |

---

## Project Structure

```
simulation-agent-v1/
│
├── main.py                        # FastAPI app factory + lifespan (auto-creates DB tables)
├── graph.py                       # LangGraph StateGraph + SSE stream_simulation()
├── streamlit_app.py               # Streamlit chat UI (local testing)
├── test_sse.html                  # Standalone SSE tester (open in browser)
├── mock_semantic_server.py        # Mock Nokia semantic API (port 8001, local dev)
├── requirements.txt
│
├── agents/
│   ├── query_refiner.py           # HITL node — interrupts for scope clarification
│   ├── orchestrator.py            # Routing node
│   ├── schema_discovery.py        # Fetches Neo4j KG schema
│   ├── planner.py                 # Decomposes query; runs N parallel traversals
│   ├── traversal.py               # Autonomous ReAct agent (8 tools)
│   └── response.py                # Synthesises data → PM report
│
├── api/
│   └── v1/
│       ├── router.py              # Aggregates all v1 routers under /api/v1
│       ├── schemas.py             # Pydantic request/response models
│       └── endpoints/
│           ├── simulate.py        # POST /simulate, POST /simulate/resume
│           ├── sse_simulate.py    # GET /simulate/stream, POST /simulate/stream/resume
│           ├── threads.py         # Thread management (list, get, delete, messages, clarification)
│           ├── health.py          # GET /health
│           ├── bkg.py             # POST /bkg (direct KG queries)
│           ├── sandbox.py         # POST /sandbox (Python execution)
│           └── semantic.py        # POST /semantic/retrieve
│
├── services/
│   ├── simulation_service.py      # Business logic: run_query, resume_query
│   ├── sse_manager.py             # asyncio.Queue + threading.Event for SSE
│   ├── db_service.py              # PostgreSQL persistence (threads, queries, HITL records)
│   ├── semantic_service.py        # Nokia semantic search API client
│   ├── bkg_service.py             # Neo4j BKG service
│   └── sandbox_service.py        # Python sandbox service
│
├── prompts/
│   ├── query_refiner_prompt.py    # HITL scope validation prompt
│   ├── orchestrator_prompt.py     # Routing classification prompt
│   ├── planner_prompt.py          # Sub-query decomposition prompt
│   ├── traversal_prompt.py        # ReAct data-gathering prompt
│   └── response_prompt.py        # PM report synthesis prompt
│
├── models/
│   └── state.py                   # SimulationState TypedDict
│
├── tools/
│   ├── langchain_tools.py         # LangChain tool wrappers (8 tools)
│   ├── neo4j_tool.py              # Neo4j connection + Cypher execution
│   ├── bkg_tool.py                # BKG query tool
│   └── python_sandbox.py         # Python sandbox executor
│
├── config/
│   └── settings.py                # Pydantic settings (Neo4j, PostgreSQL, LLM, Semantic API)
│
└── scripts/
    └── ingest_scenarios.py        # Ingests simulation scenarios into semantic DB
```

---

## Setup

### Prerequisites
- Python 3.11+
- Neo4j with your knowledge graph database
- PostgreSQL with schema `pwc_simulation_agent_schema` created
- OpenAI API key
- Nokia Semantic Search API (or use `mock_semantic_server.py` locally)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
Create a `.env` file in the project root:

```env
# OpenAI
OPENAI_API_KEY=sk-your-key
LLM_MODEL=gpt-4o

# Neo4j
NEO4J_URI=neo4j://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
NEO4J_DATABASE=nokia_syn_v1

# PostgreSQL
PG_HOST=localhost
PG_PORT=5433
PG_DATABASE=nokia_syn_v1
PG_USER=postgres
PG_PASSWORD=your-password

# Nokia Semantic Search API
SEMANTIC_SEARCH_URL=http://localhost:8001
```

### 3. Run the backend
```bash
uvicorn main:app --reload --port 8000
```

Tables in `pwc_simulation_agent_schema` are created automatically at startup.

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### 4. Run the Streamlit UI (optional)
```bash
streamlit run streamlit_app.py
```
Enter your User ID in the sidebar before chatting.

### 5. Local mock semantic server
```bash
python mock_semantic_server.py   # starts on port 8001
```

---

## API Overview

Base URL: `http://localhost:8000/api/v1`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Neo4j, PostgreSQL, OpenAI service status |
| `POST` | `/simulate` | Run a query (blocking, full response) |
| `POST` | `/simulate/resume` | Resume a HITL-paused simulation |
| `GET` | `/simulate/stream` | **SSE** — stream simulation progress events |
| `POST` | `/simulate/stream/resume` | Resume a paused SSE stream |
| `GET` | `/threads?user_id=` | List all threads for a user |
| `GET` | `/threads/{thread_id}` | Thread metadata |
| `DELETE` | `/threads/{thread_id}` | Delete thread and all its data |
| `GET` | `/threads/{thread_id}/messages` | All queries within a thread |
| `GET` | `/threads/{thread_id}/clarification` | HITL pause status (page refresh detection) |

For detailed request/response schemas and SSE event reference, see [FRONTEND_INTEGRATION_GUIDE.md](FRONTEND_INTEGRATION_GUIDE.md).

---

## Database Schema

Three tables in `pwc_simulation_agent_schema`:

```
threads              — one row per conversation thread
  thread_id, user_id, created_at, last_active_at, status

queries              — one row per user query
  query_id, thread_id, user_id, original_query, refined_query,
  routing_decision, planning_rationale (JSONB), final_response,
  started_at, completed_at, duration_ms, status

hitl_clarifications  — one row per HITL pause/resume cycle
  clarification_id, query_id, thread_id,
  questions_asked (JSONB), assumptions_offered (JSONB),
  user_answer, asked_at, answered_at, was_skipped
```

---

## HITL Flow

The **Query Refiner** agent checks whether the user's query has enough scope (market, timeframe, target volume) to run a meaningful simulation. If not:

**Standard HTTP:**
```
POST /simulate → { status: "clarification_needed", thread_id, clarification: { questions, assumptions } }
(user answers)
POST /simulate/resume { thread_id, clarification: "user answer" } → { status: "complete", final_response }
```

**SSE (stream stays open):**
```
GET /simulate/stream → event: hitl_start { questions, assumptions }
(SSE connection stays open — user answers)
POST /simulate/stream/resume { thread_id, clarification }
→ event: hitl_complete → ... → event: complete
```

---

## Example Queries

```
"Share me the weekly plan for Chicago market to complete 100 sites in next 3 weeks"
"How many GC crews are needed to complete 300 sites in Chicago in 2 weeks?"
"Recover the delayed Dallas rollout — give me a realistic plan to meet the target date"
"What is the impact if 20% of GC resources are unavailable this week?"
"How many sites are blocked by missing NTP in Chicago?"
"What is the current WIP count for the Dallas market?"
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| LangGraph `MemorySaver` checkpointer | Required for HITL `interrupt()` / `Command(resume=...)` across HTTP requests — thread state is keyed by `thread_id` |
| Module-level `_graph` singleton | All requests share one compiled graph and one MemorySaver |
| `threading.Event` for SSE HITL | Blocks executor thread (not the event loop) during HITL pause — keeps SSE connection alive |
| Per-operation DB connections | DB errors are logged but never raised — DB failures never block the agent response |
| `ThreadPoolExecutor` for parallel traversal | Planner's N sub-queries run concurrently — reduces total latency |
| Semantic context injection | KPI definitions, question bank, and past simulation scenarios are retrieved and injected into Planner and Traversal prompts |

Sequence of Information Flow
1. User submits question in chat interface
2. Query refinement extracts parameters
3. System checks for missing information
4. Orchestrator classifies question type
5. Knowledge graph schema is analyzed
6. Planner breaks question into sub-queries
7. Sub-queries run in parallel
8. Data retrieved from graph + databases
9. Results aggregated
10. Natural language response generated
11. Final answer returned to user

User → Chat Interface → Query Parser → Orchestrator
Orchestrator → Knowledge Graph
Orchestrator → SQL/Python Tools
Tools → Results
Results → Response Generator
Response Generator → User