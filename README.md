# 🎯 Simulation Agent — LangGraph Multi-Agent System

A LangGraph-based multi-agent system that traverses a Neo4j Business Knowledge Graph (BKG) to answer simulation queries for telecom project management.

## Architecture

```
User Query
    │
    ▼
┌─────────────────────┐
│  Schema Discovery    │ ← Discovers Neo4j KG schema once
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│   Planner Agent     │ ← Breaks query into Cypher/Python steps
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Traversal Agent    │ ← Executes steps against Neo4j + Python sandbox
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Orchestrator Agent │ ← Evaluates results → re-plan or proceed?
└─────────┬───────────┘
      ┌───┴───┐
      ▼       ▼
  (re-plan) (proceed)
      │       │
      ▼       ▼
  Planner  ┌─────────────────────┐
           │  Response Agent     │ ← Calculates + generates PM-readable output
           └─────────┬───────────┘
                     ▼
              Simulation Result
```

## Components

| Component | Type | Role |
|-----------|------|------|
| **Orchestrator** | LangGraph conditional router | Decides: re-plan or proceed to response |
| **Planner** | LLM Agent | Decomposes query into executable steps (Cypher, Python) |
| **Traversal** | Executor | Runs Cypher against Neo4j, Python in sandbox, with retry |
| **Response** | LLM Agent | Interprets data, runs calculations, generates PM output |
| **Schema Discovery** | Tool | Auto-discovers KG schema for Planner context |

## Project Structure

```
simulation_agent/
├── __init__.py
├── __main__.py              # python -m simulation_agent
├── main.py                  # CLI entry point
├── graph.py                 # LangGraph definition (the core)
├── requirements.txt
├── .env.example
│
├── agents/
│   ├── orchestrator.py      # Orchestrator + routing logic
│   ├── planner.py           # Planner Agent
│   ├── traversal.py         # Traversal Agent
│   ├── response.py          # Response Agent
│   └── schema_discovery.py  # KG schema discovery
│
├── config/
│   └── settings.py          # Configuration (Neo4j, LLM, etc.)
│
├── models/
│   └── state.py             # Shared LangGraph state definition
│
├── prompts/
│   └── agent_prompts.py     # All agent prompts (centralized)
│
└── tools/
    ├── neo4j_tool.py        # Neo4j connection + query execution
    └── python_sandbox.py    # Safe Python code execution
```

## Setup

### 1. Prerequisites
- Python 3.11+
- Neo4j running with your `nokia-v-one` database
- OpenAI API key (for GPT-4o)

### 2. Install Dependencies
```bash
cd simulation_agent
pip install -r requirements.txt
```

### 3. Configure Environment
```bash
cp .env.example .env
# Edit .env with your actual keys
```

Or export directly:
```bash
export OPENAI_API_KEY="sk-your-key"
export NEO4J_URI="neo4j://127.0.0.1:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="password"
export NEO4J_DATABASE="nokia-v-one"
```

### 4. Run

**Interactive mode:**
```bash
python -m simulation_agent --interactive
```

**Single query:**
```bash
python -m simulation_agent "How many sites are in Chicago and what is their status breakdown?"
```

**View KG schema:**
```bash
python -m simulation_agent --schema
```

## How It Works

### Step-by-step Flow

1. **Schema Discovery** — Queries Neo4j for node labels, relationships, properties, and counts. This gives the Planner context to write accurate Cypher.

2. **Planner Agent** — The LLM analyzes the user query + schema and produces a plan:
   ```json
   {
     "steps": [
       {"step_id": 1, "action": "cypher_query", "query_or_code": "MATCH (s:Site)...", "depends_on": []},
       {"step_id": 2, "action": "python_compute", "query_or_code": "result = ...", "depends_on": [1]}
     ]
   }
   ```

3. **Traversal Agent** — Executes steps in dependency order:
   - `cypher_query` → runs against Neo4j (read-only, with retry + LLM-assisted fix)
   - `python_compute` → runs in sandboxed Python with data from prior steps
   - `aggregate` → combines results from multiple steps

4. **Orchestrator** — Evaluates results:
   - All steps succeeded → route to Response
   - >70% succeeded → route to Response with partial data
   - Failures + iterations < 3 → route back to Planner (re-plan)
   - Max iterations → force Response

5. **Response Agent** — Synthesizes all data into a PM-readable report with tables, feasibility assessment, and recommendations.

## Extending

### Adding HITL (Query Refiner)
The graph is designed for easy extension. To add HITL:
1. Create `agents/query_refiner.py`
2. Add a `query_refiner` node to `graph.py`
3. Add an interrupt before planning: `graph.add_edge("query_refiner", "planner")`
4. Use LangGraph's `interrupt()` for human input

### Adding Simulation Models
The Response Agent can be extended to call deterministic simulation models:
1. Add model classes in `models/` (ScheduleSimulator, CapacityModel, etc.)
2. The Response Agent's Python sandbox can import and run them
3. Or add a dedicated `simulation_engine` node between Traversal and Response

### Swapping LLMs
Edit `config/settings.py` to use a different model:
```python
LLM_MODEL=claude-sonnet-4-20250514  # or any LangChain-compatible model
```

## Example Queries

```
"How many sites are in the Chicago market?"
"What is the current crew capacity by vendor?"
"Show me the prerequisite completion status for all markets"
"What is the site status breakdown by market and stage?"
"Which vendors have the highest crew count?"
```
Knowledge-Driven : Every query is grounded in the Business Knowledge Graph (BKG) — the agent traverses real Neo4j nodes and relationships rather than relying on static rules or hardcoded logic

Dynamic Schema Awareness : Schema Discovery node fetches live BKG node labels, relationship types, and properties at runtime — Traversal and Planner agents always work with the current graph structure, no manual schema maintenance

Graph-Native Querying : Traversal Agent is a ReAct agent that autonomously generates and executes Cypher queries — it reasons over the BKG iteratively (up to 20 steps), following relationship paths to gather exactly what the query needs

Parallel Graph Exploration : For complex simulation queries, Planner decomposes into N independent sub-queries and runs N Traversal agents concurrently — each explores a different slice of the BKG simultaneously, reducing total data-gathering time

Semantic + Structural Fusion : BKG structural data (Cypher results, graph paths) is combined with semantic context (KPI definitions, past simulation scenarios, question bank) — answers are grounded in both the graph topology and domain knowledge

Path-Aware Reasoning : Traversal agent can discover and follow multi-hop relationship paths in the BKG (e.g. Site → GC Crew → Market → Region) — surfaces non-obvious dependencies that flat SQL queries would miss

No Schema Lock-in : If the BKG evolves (new node types, new relationships), the Schema Discovery node auto-picks up changes on the next run — no agent code changes required

Resilient Graph Access : BKG query failures are caught per tool call and logged — the agent continues with partial data rather than failing the entire simulation
