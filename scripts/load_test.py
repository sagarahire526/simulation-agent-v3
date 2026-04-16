"""
Load test for the SSE simulation endpoints (simulate/stream + simulate/stream/resume).

Mirrors the real UI flow:
  1. GET  /simulate/stream        → opens SSE connection, receives events
  2. If hitl_start event arrives  → POST /simulate/stream/resume with clarification
  3. Continue reading SSE until complete/error

Usage:
    python scripts/load_test.py --users 5          # start with 5
    python scripts/load_test.py --users 20         # scale up
    python scripts/load_test.py --users 50 --auto-resume
"""
import argparse
import asyncio
import json
import time
import uuid

import aiohttp


BASE_URL = "http://localhost:8000/api/v1"

SAMPLE_QUERIES = [
    "How many sites are in Chicago market?",
    "What is the average cycle time for NTM projects in Chicago markets?",
    "Show me the top 5 markets by site count Chicago markets",
    "What is the completion rate for AHLOB Modernization Chicago markets",
    "How many crews are assigned to the Northeast region Chicago markets?",
]


async def stream_user_session(
    session: aiohttp.ClientSession,
    user_idx: int,
    auto_resume: bool = True,
) -> dict:
    """
    Simulates a single UI user session:
      1. Opens SSE stream via GET /simulate/stream
      2. Collects all events
      3. If HITL pause occurs and auto_resume is True, sends POST /simulate/stream/resume
      4. Continues reading until complete or error
    """
    query = SAMPLE_QUERIES[user_idx % len(SAMPLE_QUERIES)]
    params = {
        "query": f"{query} (user-{user_idx})",
        "user_id": f"load-test-user-{user_idx}",
        "project_type": "NTM",
    }

    t0 = time.perf_counter()
    events = []
    thread_id = None
    hitl_resumed = False
    final_status = "unknown"

    try:
        async with session.get(
            f"{BASE_URL}/simulate/stream",
            params=params,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                return {
                    "user": user_idx,
                    "status": resp.status,
                    "elapsed_s": round(time.perf_counter() - t0, 2),
                    "error": f"HTTP {resp.status}",
                }

            current_event = ""
            current_data = ""

            async for raw_line in resp.content:
                line = raw_line.decode("utf-8").rstrip("\n\r")

                # SSE format: "event: <name>\ndata: <json>\n\n"
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    current_data = line.split(":", 1)[1].strip()
                elif line == "" and current_event:
                    # End of SSE message
                    data = {}
                    if current_data:
                        try:
                            data = json.loads(current_data)
                        except json.JSONDecodeError:
                            data = {"raw": current_data}

                    events.append({"event": current_event, "data": data})

                    # Capture thread_id from stream_started
                    if current_event == "stream_started":
                        thread_id = data.get("thread_id")

                    # Handle HITL: auto-resume with "Accept stated assumptions"
                    if current_event == "hitl_start" and auto_resume and thread_id:
                        hitl_resumed = True
                        await asyncio.sleep(0.5)  # simulate user thinking
                        try:
                            async with session.post(
                                f"{BASE_URL}/simulate/stream/resume",
                                json={
                                    "thread_id": thread_id,
                                    "clarification": "Accept stated assumptions",
                                },
                                timeout=aiohttp.ClientTimeout(total=30),
                            ) as resume_resp:
                                if resume_resp.status != 200:
                                    events.append({
                                        "event": "_resume_error",
                                        "data": {"status": resume_resp.status},
                                    })
                        except Exception as e:
                            events.append({
                                "event": "_resume_error",
                                "data": {"error": str(e)[:100]},
                            })

                    # Stream done
                    if current_event in ("complete", "error"):
                        final_status = current_event
                        break

                    current_event = ""
                    current_data = ""

        elapsed = round(time.perf_counter() - t0, 2)
        event_names = [e["event"] for e in events]

        return {
            "user": user_idx,
            "status": 200,
            "elapsed_s": elapsed,
            "events_received": len(events),
            "event_flow": " → ".join(event_names),
            "final_status": final_status,
            "hitl_resumed": hitl_resumed,
            "thread_id": thread_id,
        }

    except asyncio.TimeoutError:
        return {
            "user": user_idx,
            "status": "timeout",
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "error": "Request timed out (600s)",
        }
    except Exception as e:
        return {
            "user": user_idx,
            "status": "error",
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "error": str(e)[:150],
        }


async def main(num_users: int, auto_resume: bool):
    print(f"\n{'='*60}")
    print(f"  SSE Load Test: {num_users} concurrent users")
    print(f"  Endpoints: GET /simulate/stream")
    if auto_resume:
        print(f"             POST /simulate/stream/resume (on HITL)")
    print(f"{'='*60}\n")

    connector = aiohttp.TCPConnector(limit=num_users + 10)
    async with aiohttp.ClientSession(connector=connector) as session:
        t0 = time.perf_counter()
        tasks = [
            stream_user_session(session, i, auto_resume=auto_resume)
            for i in range(num_users)
        ]
        results = await asyncio.gather(*tasks)
        total_time = round(time.perf_counter() - t0, 2)

    # ── Per-user details ─────────────────────────────────────────
    print(f"  {'User':<8} {'Status':<10} {'Time':>8} {'Events':>7}  {'HITL':>5}  Flow")
    print(f"  {'─'*80}")
    for r in sorted(results, key=lambda x: x["user"]):
        user = f"user-{r['user']}"
        status = r.get("final_status", r.get("status", "?"))
        elapsed = f"{r['elapsed_s']}s"
        events = str(r.get("events_received", "-"))
        hitl = "yes" if r.get("hitl_resumed") else "no"
        flow = r.get("event_flow", r.get("error", ""))
        # Truncate flow for readability
        if len(flow) > 60:
            flow = flow[:57] + "..."
        print(f"  {user:<8} {status:<10} {elapsed:>8} {events:>7}  {hitl:>5}  {flow}")

    # ── Summary ──────────────────────────────────────────────────
    successes = [r for r in results if r.get("final_status") == "complete"]
    errors = [r for r in results if r.get("final_status") != "complete"]
    hitl_count = sum(1 for r in results if r.get("hitl_resumed"))
    elapsed_times = [r["elapsed_s"] for r in results]

    print(f"\n{'─'*60}")
    print(f"  Summary")
    print(f"{'─'*60}")
    print(f"  Total wall time:   {total_time}s")
    print(f"  Completed:         {len(successes)}/{num_users}")
    print(f"  Failed/timeout:    {len(errors)}/{num_users}")
    print(f"  HITL resumed:      {hitl_count}/{num_users}")
    if elapsed_times:
        print(f"  Fastest response:  {min(elapsed_times)}s")
        print(f"  Slowest response:  {max(elapsed_times)}s")
        print(f"  Avg response:      {round(sum(elapsed_times)/len(elapsed_times), 2)}s")
    print(f"{'─'*60}\n")

    if errors:
        print("  Failed users:")
        for e in errors[:10]:
            reason = e.get("error", e.get("final_status", "unknown"))
            print(f"    user-{e['user']}: {reason}")
        if len(errors) > 10:
            print(f"    ... and {len(errors)-10} more")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SSE load test for simulation agent")
    parser.add_argument("--users", type=int, default=5, help="Number of concurrent users (default: 5)")
    parser.add_argument("--auto-resume", action="store_true", default=True, help="Auto-resume HITL pauses (default: True)")
    parser.add_argument("--no-auto-resume", dest="auto_resume", action="store_false", help="Don't auto-resume; streams will hang at HITL")
    args = parser.parse_args()

    asyncio.run(main(args.users, args.auto_resume))
