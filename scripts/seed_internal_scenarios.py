"""
One-time seeder for the Internal Scenario Library — populates
data/internal_scenarios.json with the 15 program-office scenarios from the
'15 Scenarios Questions steps to solve' sheet.

Each scenario is added via services.internal_scenarios.add(), which embeds the
question with text-embedding-3-small and writes atomically. Re-running this
script appends duplicates — wipe data/internal_scenarios.json first if you want
a clean reseed.

Usage (from the simulation-agent-v1 directory):
    venv/bin/python scripts/seed_internal_scenarios.py
    venv/bin/python scripts/seed_internal_scenarios.py --reset    # wipe + reseed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from services import internal_scenarios as scenario_lib  # noqa: E402


# ── The 15 scenarios (verbatim from the program-office sheet) ────────────────

SCENARIOS: list[dict] = [
    {
        "tag": "S1 — GC reshuffle by past cycle-time performance",
        "question": (
            "For the next 6 months planned sites in central region reassign the sites of "
            "bottom 3 worst performing GCs to the top 3 best performing GCs based on "
            "past 6-month avg cycle time. Simulate the comparison of pre and post "
            "predicted Cx weekly runrate and the Cx cycle time impact for next 6 months"
        ),
        "steps": [
            "Retrieve last 3/6 months completed sites with GC, Cx start and Cx completion & SCOP submission dates for Central region",
            "Compute site-level cycle time and derive GC-wise average cycle time of Installation to SCOP Submission",
            "Rank GCs to identify top 3 fastest and bottom 3 slowest performers",
            "Pull next 6 months planned sites with current GC assignments",
            "Identify the sites assigned to the Bottom 3 GCs",
            "Reassign these sites across top 3 GCs while keeping workload distribution balanced",
            "Calculate currently assigned GCs weekly Cx run rate and average cycle time for next 6 months based on their historical performance",
            "Replace cycle time assumptions for reassigned sites using new GC performance based on their historical performance",
            "Recalculate expected completion timelines for all impacted sites",
            "Generate revised weekly completion forecast",
            "Measure change in runrate and overall cycle time after reassignment",
            "Highlight improvement or bottleneck shifts caused by redistribution & Create a comparison table",
        ],
    },
    {
        "tag": "S2 — Equipment / crane availability constraint",
        "question": (
            "If the Crane availability is limited to 2 days per week from today, what "
            "is the revised plan for those sites which require Crane in Chicago market "
            "and plan in next 2 months planned sites?"
        ),
        "steps": [
            "Retrieve all planned sites for next 2 months in Chicago",
            "Identify crane required sites from the current status & historical data",
            "Map current schedule of these sites week-wise",
            "Identify crane available sites count week wise",
            "Compare planned crane demand against available capacity",
            "Identify weeks where crane demand exceeds supply",
            "Based on the current & predicted pre-requisites readiness status, assign crane to potential ready sites",
            "Push overflow sites to next feasible crane slots",
            "Reorder execution based on readiness and priority",
            "Recalculate start and completion dates for impacted sites",
            "Build revised weekly execution plan",
            "Quantify delay and backlog created due to crane constraint [ Suggest : Utilize freed crew capacity by advancing non-crane sites where possible ]",
        ],
    },
    {
        "tag": "S3 — Weather thresholds (wind / rain / temp) revised plan",
        "question": (
            "Consider wind>25m/hr ,Rain>20mm, Temp<10C are non suitable to work, "
            "Predict the weather forecast and simulate the revised plan in Detroit "
            "market in next 3 months"
        ),
        "steps": [
            "Retrieve planned sites for next 3 months in Detroit",
            "Obtain weather forecast data for the same period (From DB or from open-source API)",
            "Identify days violating working thresholds for wind, rain and temperature",
            "Map non-working days against scheduled site activities",
            "Flag sites planned on weather-restricted days",
            "Shift impacted sites to nearest feasible working days",
            "Re-sequence site execution based on dependencies & pre-requisites",
            "Adjust crew allocation to align with revised schedule",
            "Recalculate weekly execution output",
            "Estimate cumulative delay introduced by weather disruptions",
            "Compare productivity before and after weather adjustment",
        ],
    },
    {
        "tag": "S4 — Material / hardware inventory hard cap (e.g. AHLOB 3000 swaps)",
        "question": (
            "AHLOB hardware inventory in TMO MSL WH is sufficient for 3000 swaps, "
            "Simulate market wise & GC wise Swap plan based on other prerequisites "
            "for the next 6 months"
        ),
        "steps": [
            "Retrieve total planned swap sites for next 6 months across markets",
            "Validate total available inventory against 3000 swap limit",
            "Identify sites that will be ready excluding hardware/material dependency based on current status & historical pattern-based prediction",
            "Segment potential ready sites by market and GC",
            "Prioritize sites based on readiness and execution feasibility",
            "Distribute allocation across markets proportional to demand and readiness & map the assigned GCs",
            "Assign swap timelines aligned with prerequisite readiness",
            "Identify sites left unplanned due to inventory gap",
            "Generate market-wise and GC-wise execution plan for next months & Provide the week wise feasible, material allotted sites list",
        ],
    },
    {
        "tag": "S5 — SCOP acceptance prediction from cycle-time trend",
        "question": (
            "Based on the cycle time trend between SCOP acceptance for last 6 months "
            "in South region predict the SCOP acceptance date for pending acceptance sites"
        ),
        "steps": [
            "Retrieve last 6 months SCOP acceptance completed sites in South region",
            "Calculate cycle time between SCOP submission and acceptance & Cx Complete & SCOP Acceptance",
            "Retrieve all Cx Completed sites with pending SCOP acceptance",
            "Derive expected cycle time based on the cycle time calculated",
            "Predict acceptance date using the current status of the site & predicted cycle time",
            "Provide weekly predicted SCOP acceptance run rate for South [Provide the sites with expected acceptance date)",
            "Flag sites with abnormal delay risk",
        ],
    },
    {
        "tag": "S6 — Swap target increase (extra N) on top of existing monthly target",
        "question": (
            "If targeted sites swap increase by 200 in next month on top of current "
            "800 sites/month target, how should we reschedule the existing planned "
            "sites based on the available prerequisites and simulate the revised plan"
        ),
        "steps": [
            "Identify all currently planned sites for next month and confirm if total planned volume equals 800 sites",
            "If planned sites are less than 800, pull additional ready sites from backlog to complete the 800 baseline plan",
            "Once baseline 800 plan is confirmed, add additional 200 target sites into the plan",
            "Retrieve prerequisite readiness status for all sites in the combined pool",
            "Retrieve current GC-wise crew capacity",
            "Calculate effective productivity using recent swap completion trends",
            "Convert total ready site demand into required crew capacity",
            "Compare required crews against available GC-wise capacity",
            "Identify capacity shortfall at overall level & per GC",
            "Prioritize site allocation based on readiness & execution feasibility",
            "Lock high-priority ready sites into early weekly schedule slots",
            "Allocate remaining capacity to additional 200 sites where feasible",
            "Build revised week-wise execution schedule for next month",
            "Quantify achievable sites & Highlight capacity gaps, prerequisite bottlenecks and execution risks",
        ],
    },
    {
        "tag": "S7 — Revenue recognition milestones forecast",
        "question": (
            "How many sites will cross revenue recognition milestones (Civil Cx ,Tower "
            "Cx Complete and SCOP acceptance) based on last 6 month project performance "
            "in next 3 months for New Build sites"
        ),
        "steps": [
            "Retrieve last 6 months Civil Cx ,Tower Cx Complete and SCOP acceptance completed sites for New Build sites",
            "Calculate average time taken to move between each milestone",
            "Identify current status of all active and planned sites",
            "Project forward movement using historical cycle time durations",
            "Estimate milestone completion dates for each site",
            "Aggregate milestone achievements over next 3 months (3 key payment Milestone wise)",
            "Calculate expected weekly milestone completions",
            "Highlight potential gaps in revenue recognition pipeline",
        ],
    },
    {
        "tag": "S8 — Remove top-3 worst-performing GCs and simulate pre/post impact",
        "question": (
            "Remove the top 3 worst performing GCs (based on last 3 months performance) "
            "from the project and simulate pre and post impact on performance metrics "
            "for next 3 months planned sites"
        ),
        "steps": [
            "Retrieve last 3 months GC performance data including SCOP FTR, Average punch point per site, and SCOP submission cycle time",
            "Rank GCs based on the derived metrics & identify bottom 3 performers",
            "Retrieve next 3 months planned sites & identify how many sites are assigned to the bottom 3 GCs",
            "Reassign those sites to top performing GCs considering workload balance",
            "Capture baseline performance metrics before reassignment",
            "Calculate the performance metrics after new GCs reassignment",
            "Compare pre and post-performance outcomes",
            "Prepare a comparative table of the impacted sites with Old/New GC & performance metrics for both GCs & highlight the difference",
        ],
    },
    {
        "tag": "S9 — Complete all Cx-pending sites in a region within N months",
        "question": (
            "If I want to complete all Cx pending sites for Central region within next "
            "3 months, simulate the weekly project plan and required GC wise crew "
            "capacity addition on top on existing crew"
        ),
        "steps": [
            "Retrieve the total number of sites & GC assignment status in the Central & Segment the sites (Completed, Ongoing)",
            "For GC assignment pending sites, assign/distribute the sites to top 3 vendors",
            "Identify ideal weekly completion rate for Central based on past performance",
            "Derive required weekly completion rate to meet 3-month deadline",
            "Retrieve current GC-wise crew capacity",
            "Compare required vs available execution capacity",
            "Identify capacity shortfall",
            "Estimate additional crews needed per GC based on their site assignment",
            "Reallocate workload based on enhanced capacity",
            "Identify prerequisite readiness for each pending/ongoing site",
            "Build weekly execution schedule to meet target (Maximum pre-requisites ready sites in earlier weeks)",
            "Highlight risks and over-utilization points",
        ],
    },
    {
        "tag": "S10 — Material handover plan with optional fasttrack %",
        "question": (
            "Simulate a market wise AHLOA material handover plan for 8,000 radios from "
            "swap completed sites to MSL WH. Show baseline volume and handover dates by "
            "market, then simulate a 20% Fasttrack in swap completion and provide the "
            "revised handover plan. Compare pre and post fasttrack scenarios, "
            "highlighting differences in handover volume and timeline by each market."
        ),
        "steps": [
            "Retrieve last 2 months data for AHLOA swap completion and AHLOA radio handover trend / month in all market",
            "Identify the total number of swaps completed sites in all market / month & Total no of radios deposited to MSL WH by all market / month & pending swap required sites to deposit 8000 radios in all markets.",
            "Fasttrack 20% sites from current swap run rate/week, identify the pre-requisites & material & GC Crew Availability from all market.",
            "Compare the pre & post fasttrack scenario with baseline plan Vs incremental baseline plan for the pending swap sites and pending radio deposit.",
            "Highlight the differences in radio handover volume and timeline by each market.",
        ],
    },
    {
        "tag": "S11 — N-day delay in a cycle phase (e.g. Entitlement → MSL pickup) for WIP sites",
        "question": (
            "There is a 6 day delay in cycle time between Entitlement and MSL pickup "
            "for the WIP sites in South region, simulate market wise daily Cx start "
            "run rate based on current status for 2 months"
        ),
        "steps": [
            "Retrieve WIP sites for South region",
            "Assess the current phase of the WIP Sites",
            "For entitlement completed but material pick-up pending sites, add additional 6-day delay based on current delay (Today date – Entitlement completion date)",
            "Update material readiness timeline based on the delay",
            "Recalculate Cx start dates for impacted sites based on other critical pre-requisites",
            "Generate daily Cx start schedule for next 2 months",
            "Aggregate daily run rate marketwise",
            "Compare with original run rate before delay",
        ],
    },
    {
        "tag": "S12 — Crew productivity baseline + N% volume increase",
        "question": (
            "Based on the last 2-month trend of crew capacity/productivity of swap "
            "completion, simulate the crew requirement for next 4 weeks scheduled "
            "sites if there is an increase of 15% sites in each week"
        ),
        "steps": [
            "Retrieve last 2 months data for swap completion including crew count and sites completed in each region",
            "Calculate crew productivity as sites completed per crew per week",
            "Analyse productivity trend to identify average and variability",
            "Retrieve next 4 weeks scheduled swap sites",
            "Apply 15% increase to site volume for each week",
            "Compute total expected crew per week after increase using productivity benchmarks",
            "Compare required crews against current crew availability",
            "Identify weekly crew shortages or surplus",
            "Generate revised crew requirement per week",
            "Highlight peak weeks with maximum crew deficit",
            "Provide Region wise capacity gap summary across 4 weeks",
        ],
    },
    {
        "tag": "S13 — Repeated punch points → vendor training plan",
        "question": (
            "Analyse the repeated punch points from the last 3 months. Predict which "
            "specific areas of work require immediate vendor training to eliminate the "
            "repeated punch points in this month, and the number of crews that need to "
            "be trained"
        ),
        "steps": [
            "Retrieve last 3 months punch point data including site, GC",
            "Identify repeated punch points made by each GC",
            "Group repeated punch points by category or work area",
            "Identify GCs and crews associated with these repeated issues",
            "Calculate number of crews contributing to majority of repeated issues",
            "Estimate number of crews that must be trained to achieve desired reduction",
            "Prioritize training focus areas based on impact and frequency",
            "Generate targeted vendor training plan with crew coverage",
        ],
    },
    {
        "tag": "S14 — N% crew shortage in a region for N weeks",
        "question": (
            "If there is a crew shortage of 15% in South region due to seasonal labor "
            "migration over the next 6 weeks, simulate the revised weekly Cx start "
            "runrate and the impact on scheduled sites in that region"
        ),
        "steps": [
            "Retrieve current scheduled sites and weekly Cx start plan for South region over next 6 weeks",
            "Retrieve current crew capacity and allocation across GCs",
            "Calculate baseline weekly Cx start runrate",
            "Apply 15% reduction to total available crews",
            "Translate reduced crew capacity into execution capability",
            "Recalculate maximum possible Cx starts per week under reduced capacity",
            "Compare planned vs achievable Cx starts week-wise",
            "Identify backlog created each week due to shortage",
            "Accumulate backlog across the 6-week period",
            "Push excess sites to subsequent weeks based on capacity availability & other pre-requisite readiness",
            "Re-sequence site execution based on readiness and priority",
            "Recalculate updated weekly run rate after adjustment",
            "Estimate total delay introduced across all sites",
            "Identify weeks with highest execution drop & overall impact on schedule completion",
        ],
    },
    {
        "tag": "S15 — Project plan with explicit pace / material / crew constraints",
        "question": (
            "Prepare a project plan for 4500 sites considering a pace of 150 sites in "
            "a week & material availability of 250 sites for first 3 weeks and 200 "
            "sites per week from week 4 onwards with a constraint of crews "
            "( Central - 23, South -12, West -9 )"
        ),
        "steps": [
            "Identify all not completed sites & assess the pre-requisites status",
            "Forecast the pre-requisites completion based on SLA or historical pattern",
            "Identify Historical patterns of the GCs in that market to identify possible delays & risks",
            "Identify if any additional GCs/Crews required",
            "Forecast the Completion time based on historical pattern",
            "Create a project plan for all not completed sites",
            "Align weekly execution plan with crew productivity vs material availability constraints (Provide justification if constraints are not aligned)",
        ],
    },
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true",
                   help="Wipe data/internal_scenarios.json before seeding.")
    args = p.parse_args()

    data_file = Path(_PROJECT_ROOT) / "data" / "internal_scenarios.json"
    if args.reset and data_file.is_file():
        with data_file.open("w", encoding="utf-8") as f:
            json.dump({"version": 1, "embedding_model": "text-embedding-3-small", "scenarios": []}, f, indent=2)
        print(f"⌫  Reset {data_file}\n")

    print(f"Seeding {len(SCENARIOS)} scenarios into {data_file}\n")
    for i, sc in enumerate(SCENARIOS, 1):
        print(f"  [{i:>2}/{len(SCENARIOS)}] {sc['tag']}", flush=True)
        try:
            saved = scenario_lib.add(sc["tag"], sc["question"], sc["steps"])
            print(f"           ✓ id={saved['id']}  ({len(sc['steps'])} steps)")
        except Exception as exc:  # noqa: BLE001
            print(f"           ✗ failed: {exc}")
            return 1
    print(f"\nDone. Library now contains {len(scenario_lib.list_all())} scenarios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
