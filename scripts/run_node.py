"""
scripts/run_node.py — execute a BKG node's stored python function in ISOLATION.

Runs a node's kpi/map/scn function against the real (read-only) Postgres using the
SAME sandbox helpers production uses (run_node / run_transform / run_scenario), so
you can verify a node's logic + data WITHOUT the agent flow. The generated SQL is
printed by the sandbox's own `🔎 execute_query` logging as it runs.

Modes
-----
  node       KPI / core node — calls its get_*(execute_query, filters) function.
  scenario   Scenario orchestrator — chains its contributing nodes deterministically.
  transform  Pure transform node — calls predict_/transform_/compute_(*args, **kwargs).

Examples
--------
  # SCOP cycle-time KPI, FTR baseline, grouped by market, last 6 months in South:
  python -m scripts.run_node node 109ef604-2e52-4082-8ebe-d4297e9daa52 \
      --filters '{"rgn_region":"SOUTH","smp_name":"NTM","start_date":"2026-01-07","end_date":"2026-07-07","ftr_only":true}' \
      --group-by m_market

  # Pending SCOP acceptance sites:
  python -m scripts.run_node node pending_scop_acceptance_sites \
      --filters '{"rgn_region":"SOUTH","smp_name":"NTM"}'

  # Full scenario (matches what the planner bypass runs):
  python -m scripts.run_node scenario scn-001-scop-acceptance-prediction \
      --filters '{"rgn_region":"SOUTH","smp_name":"NTM","start_date":"2026-01-07","end_date":"2026-07-07"}' \
      --group-by m_market

  # Pure transform (predictor) with positional JSON args + kwargs:
  python -m scripts.run_node transform 41587967-8a03-4503-908b-de7642c5c3ad \
      --args '[[], []]' --kwargs '{"group_by":"m_market"}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

# Make the project root importable when run directly (python scripts/run_node.py …).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.sandbox_service as sandbox_svc  # noqa: E402


def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=_default)


def _print_result(result, limit: int) -> None:
    if isinstance(result, list):
        print(f"\n▶ {len(result)} row(s) returned")
        print(_dump(result[:limit]))
        if len(result) > limit:
            print(f"… ({len(result) - limit} more row(s) hidden — raise --limit)")
    elif isinstance(result, dict):
        summary = {k: (f"<{len(v)} rows>" if isinstance(v, list) else v)
                   for k, v in result.items()}
        print("\n▶ result (top level):")
        print(_dump(summary))
        for k, v in result.items():
            if isinstance(v, list) and v:
                print(f"\n▶ {k}: first {min(limit, len(v))} of {len(v)} row(s)")
                print(_dump(v[:limit]))
    else:
        print("\n▶ result:")
        print(_dump(result))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Execute a BKG node's stored python function in isolation.")
    ap.add_argument("mode", choices=["node", "scenario", "transform"])
    ap.add_argument("node_id", help="BKGNode node_id (e.g. 109ef604-… or pending_scop_acceptance_sites)")
    ap.add_argument("--filters", default="{}", help="JSON dict of filters (node/scenario)")
    ap.add_argument("--group-by", default=None, help="group_by dimension (node/scenario)")
    ap.add_argument("--args", default="[]", help="JSON list of positional args (transform)")
    ap.add_argument("--kwargs", default="{}", help="JSON dict of kwargs (transform)")
    ap.add_argument("--limit", type=int, default=25, help="max rows to print (default 25)")
    ap.add_argument("--timeout", type=int, default=120, help="sandbox timeout seconds")
    args = ap.parse_args()

    print(f"⚙  mode={args.mode}  node={args.node_id}", flush=True)
    try:
        # Same service layer the /bkg-nodes/execute endpoint uses — single code path.
        out = sandbox_svc.run_bkg_node(
            args.mode,
            args.node_id,
            filters=json.loads(args.filters),
            group_by=args.group_by,
            args=json.loads(args.args),
            kwargs=json.loads(args.kwargs),
            timeout_seconds=args.timeout,
        )
    except json.JSONDecodeError as e:
        print(f"✖ bad JSON in an argument: {e}")
        sys.exit(2)
    except ValueError as e:
        print(f"✖ {e}")
        sys.exit(2)

    if out.get("status") != "success":
        print(f"\n✖ execution error: {out.get('error')}")
        sys.exit(1)
    _print_result(out.get("result"), args.limit)


if __name__ == "__main__":
    main()
