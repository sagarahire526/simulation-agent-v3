"""
Planner-only test — runs the planning step in isolation, without spawning any
traversals or response synthesis. Use this to iterate on the planner prompt
quickly: ~30s per run vs minutes for the full pipeline.

Usage (from the simulation-agent-v1 directory):
    venv/bin/python scripts/test_planner.py "your query here"
    venv/bin/python scripts/test_planner.py "your query" --json

    # Batch mode — one query per non-empty line in the file
    venv/bin/python scripts/test_planner.py --batch queries.txt
    venv/bin/python scripts/test_planner.py --batch queries.txt --markdown > plans.md

What it does:
    1. Calls SemanticService for KPI / Q&A / scenario / keyword context
       (gracefully handles when the API is unreachable — returns empty context).
    2. Formats PLANNER_SYSTEM with today's date + the semantic context.
    3. Invokes the planner-tier LLM (gpt-5 by default).
    4. Parses and prints the rationale + sub-query list.

It does NOT touch the Knowledge Graph, traversal agents, or response agent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure the project root is importable
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from langchain_core.messages import SystemMessage, HumanMessage  # noqa: E402

from services.llm_provider import LLMProvider  # noqa: E402
from services.semantic_service import SemanticService  # noqa: E402
from services.date_context import today_date_context  # noqa: E402
from prompts.planner_prompt import PLANNER_SYSTEM  # noqa: E402
from agents.planner import _parse_planner_response  # noqa: E402

# ANSI colors (match planner_node's style)
_CYAN, _GREEN, _YELLOW, _DIM, _BOLD, _RESET = (
    "\033[96m", "\033[92m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
)


def plan_one(query: str) -> dict:
    """Run the planning step for a single query. Returns a dict with rationale,
    steps, semantic-context summary, and timing."""
    started = time.time()

    # ── Semantic context (degrades gracefully on API errors) ─────────
    semantic = SemanticService()
    try:
        ctx = semantic.get_all_context(query)
    except Exception as e:
        print(f"{_YELLOW}⚠  Semantic API failed: {e} — proceeding with empty context.{_RESET}")
        ctx = {}
    total_hits = sum(len(v) for v in ctx.values())
    semantic_context = (
        semantic.format_traversal_context(ctx) if total_hits else ""
    )

    # ── Build prompt ──────────────────────────────────────────────────
    safe_semantic = semantic_context.replace("{", "{{").replace("}", "}}")
    prompt = PLANNER_SYSTEM.format(
        today_date=today_date_context(),
        semantic_context=safe_semantic,
    )

    # ── Invoke planner LLM ────────────────────────────────────────────
    llm = LLMProvider.get_llm("planner")
    resp = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=query),
    ])

    rationale, steps = _parse_planner_response(resp.content)
    elapsed = time.time() - started

    return {
        "query": query,
        "rationale": rationale,
        "steps": steps,
        "semantic_hits": {
            "kpi":           len(ctx.get("kpi", [])),
            "question_bank": len(ctx.get("question_bank", [])),
            "simulation":    len(ctx.get("simulation", [])),
            "keywords":      len(ctx.get("keywords", [])),
        },
        "elapsed_sec": round(elapsed, 2),
        "raw_response": resp.content,
    }


def _print_human(result: dict) -> None:
    """Pretty-print one result for terminal viewing."""
    h = result["semantic_hits"]
    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}📥 Query:{_RESET} {result['query']}")
    print(f"  {_DIM}Semantic — {h['kpi']} KPI · {h['question_bank']} Q&A · "
          f"{h['simulation']} scenario · {h['keywords']} keywords"
          f"  ·  {result['elapsed_sec']}s{_RESET}")
    print(f"{_BOLD}{'═' * 70}{_RESET}\n")

    print(f"  {_YELLOW}📋 Rationale:{_RESET}")
    print(f"    {result['rationale'] or '(empty)'}\n")

    steps = result["steps"]
    print(f"  {_CYAN}📝 Sub-queries ({len(steps)}):{_RESET}")
    if not steps:
        print(f"    {_DIM}(no steps parsed — raw response below){_RESET}")
        print(f"    {result['raw_response']}")
    for i, s in enumerate(steps, 1):
        display = s.split(": ", 1)[1] if ": " in s else s
        print(f"    {_GREEN}{i}.{_RESET} {display}")
    print()


def _print_markdown(result: dict) -> str:
    """Format one result as a markdown block for batch reports."""
    h = result["semantic_hits"]
    out = [f"### Query: {result['query']}"]
    out.append(f"*Semantic: {h['kpi']} KPI · {h['question_bank']} Q&A · "
               f"{h['simulation']} scenario · {h['keywords']} keywords  ·  "
               f"{result['elapsed_sec']}s*")
    out.append("")
    out.append(f"**Rationale:** {result['rationale'] or '_(empty)_'}")
    out.append("")
    out.append(f"**Sub-queries ({len(result['steps'])}):**")
    for i, s in enumerate(result["steps"], 1):
        display = s.split(": ", 1)[1] if ": " in s else s
        out.append(f"{i}. {display}")
    out.append("")
    out.append("---")
    out.append("")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run the planner agent in isolation — no traversal, no response.",
    )
    p.add_argument("query", nargs="?", help="A single query to plan.")
    p.add_argument("--batch", help="Path to a file with one query per line.")
    p.add_argument("--json", action="store_true",
                   help="Emit raw JSON instead of human output (single-query mode).")
    p.add_argument("--markdown", action="store_true",
                   help="Emit markdown instead of human output (works in both modes).")
    args = p.parse_args()

    if not args.query and not args.batch:
        p.error("Provide either a query string or --batch <file>.")

    # ── Single query ──────────────────────────────────────────────────
    if args.query and not args.batch:
        result = plan_one(args.query)
        if args.json:
            print(json.dumps(result, indent=2))
        elif args.markdown:
            print(_print_markdown(result))
        else:
            _print_human(result)
        return 0

    # ── Batch ─────────────────────────────────────────────────────────
    path = Path(args.batch)
    if not path.is_file():
        print(f"{_YELLOW}File not found: {path}{_RESET}", file=sys.stderr)
        return 2
    queries = [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not queries:
        print(f"{_YELLOW}No queries in {path} (empty or all comments).{_RESET}",
              file=sys.stderr)
        return 2

    if args.markdown:
        print(f"# Planner test — {len(queries)} queries\n")
    for i, q in enumerate(queries, 1):
        if not args.markdown:
            print(f"\n{_BOLD}[{i}/{len(queries)}]{_RESET}")
        try:
            result = plan_one(q)
        except Exception as e:
            print(f"{_YELLOW}⚠  Failed for query {i}: {e}{_RESET}", file=sys.stderr)
            continue
        if args.markdown:
            print(_print_markdown(result))
        else:
            _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
