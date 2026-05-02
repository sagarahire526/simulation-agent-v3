"""
Embeddings endpoint — POST /api/v1/embeddings/rebuild

Runs `scripts/compose_and_embed.py` first (loads BKG nodes/edges from Neo4j,
embeds them, persists to Postgres), then `scripts/compose_paths.py`
(enumerates 1..N-hop paths, embeds them, persists to Postgres) — in that
order, in a single hit.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["Embeddings"])

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPTS_DIR = os.path.join(_PROJECT_ROOT, "scripts")
_COMPOSE_AND_EMBED = os.path.join(_SCRIPTS_DIR, "compose_and_embed.py")
_COMPOSE_PATHS = os.path.join(_SCRIPTS_DIR, "compose_paths.py")


class RebuildEmbeddingsRequest(BaseModel):
    dry_run: bool = False
    limit: int = 0          # 0 = all nodes (compose_and_embed)
    max_hops: int = 3       # path enumeration depth (compose_paths)
    cap: int = 0            # 0 = no cap (compose_paths)

    model_config = {
        "json_schema_extra": {
            "example": {
                "dry_run": False,
                "limit": 0,
                "max_hops": 3,
                "cap": 0,
            }
        }
    }


class ScriptRunResult(BaseModel):
    script: str
    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str


class RebuildEmbeddingsResponse(BaseModel):
    status: str             # "ok" | "failed"
    steps: list[ScriptRunResult]


def _run_script(script_path: str, args: list[str]) -> ScriptRunResult:
    cmd = [sys.executable, script_path, *args]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    duration = round(time.perf_counter() - t0, 2)
    return ScriptRunResult(
        script=os.path.basename(script_path),
        returncode=proc.returncode,
        duration_seconds=duration,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _rebuild(req: RebuildEmbeddingsRequest) -> RebuildEmbeddingsResponse:
    steps: list[ScriptRunResult] = []

    nodes_args: list[str] = []
    if req.dry_run:
        nodes_args.append("--dry-run")
    if req.limit:
        nodes_args.extend(["--limit", str(req.limit)])

    nodes_result = _run_script(_COMPOSE_AND_EMBED, nodes_args)
    steps.append(nodes_result)
    if nodes_result.returncode != 0:
        return RebuildEmbeddingsResponse(status="failed", steps=steps)

    paths_args: list[str] = ["--max-hops", str(req.max_hops)]
    if req.dry_run:
        paths_args.append("--dry-run")
    if req.cap:
        paths_args.extend(["--cap", str(req.cap)])

    paths_result = _run_script(_COMPOSE_PATHS, paths_args)
    steps.append(paths_result)

    overall = "ok" if paths_result.returncode == 0 else "failed"
    return RebuildEmbeddingsResponse(status=overall, steps=steps)


@router.post("/embeddings/rebuild", response_model=RebuildEmbeddingsResponse)
async def rebuild_embeddings(req: RebuildEmbeddingsRequest):
    """
    Rebuild BKG node + path embeddings in Postgres.

    Runs sequentially:
      1. `compose_and_embed.py` — fetch BKG nodes/edges from Neo4j, embed nodes,
         truncate-and-load `pwc_agent_utility_schema.nodes` + `edges`.
      2. `compose_paths.py` — enumerate 1..`max_hops` simple directed paths,
         embed, drop-and-recreate `pwc_agent_utility_schema.paths`.

    If step 1 fails, step 2 is skipped. Both scripts read DB / OpenAI
    credentials from `.env` at the project root.
    """
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _rebuild(req))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
