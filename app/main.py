"""
FastAPI application factory.

Creates the app, registers middleware, and mounts versioned routers.
No business logic lives here.

Run:
    uvicorn api.app:app --reload --port 8000

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.v1.router import router as v1_router

app = FastAPI(
    title="Simulation Agent API",
    description=(
        "LangGraph multi-agent system backed by Neo4j (BKG) and PostgreSQL.\n\n"
        "| Endpoint | Description |\n"
        "|----------|-------------|\n"
        "| `POST /api/v1/simulate` | Run a natural-language query through the full agent |\n"
        "| `POST /api/v1/bkg/query` | Query the Business Knowledge Graph directly |\n"
        "| `GET  /api/v1/schema` | ConceptNode table overview |\n"
        "| `POST /api/v1/sandbox/execute` | Run Python code against PostgreSQL |\n"
        "| `GET  /api/v1/health` | Connectivity check |\n"
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix="/api")
