"""
Simple concurrency load test for the simulation agent.

Usage:
    python scripts/load_test.py --users 10 --endpoint simulate
    python scripts/load_test.py --users 50 --endpoint stream
"""
import argparse
import asyncio
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


async def send_simulate(session: aiohttp.ClientSession, user_idx: int) -> dict:
    query = SAMPLE_QUERIES[user_idx % len(SAMPLE_QUERIES)]
    payload = {
        "query": f"{query} (user-{user_idx})",
        "user_id": f"load-test-user-{user_idx}",
        "thread_id": str(uuid.uuid4()),
        "project_type": "NTM",
    }
    t0 = time.perf_counter()
    try:
        async with session.post(f"{BASE_URL}/simulate", json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            body = await resp.json()
            elapsed = round(time.perf_counter() - t0, 2)
            return {"user": user_idx, "status": resp.status, "elapsed_s": elapsed, "response_status": body.get("status", "?")}
    except Exception as e:
        elapsed = round(time.perf_counter() - t0, 2)
        return {"user": user_idx, "status": "error", "elapsed_s": elapsed, "error": str(e)[:100]}


async def send_stream(session: aiohttp.ClientSession, user_idx: int) -> dict:
    query = SAMPLE_QUERIES[user_idx % len(SAMPLE_QUERIES)]
    params = {
        "query": f"{query} (user-{user_idx})",
        "user_id": f"load-test-user-{user_idx}",
        "project_type": "NTM",
    }
    t0 = time.perf_counter()
    events_received = 0
    last_event = ""
    try:
        async with session.get(f"{BASE_URL}/simulate/stream", params=params, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            async for line in resp.content:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("event:"):
                    last_event = decoded.split(":", 1)[1].strip()
                    events_received += 1
                    if last_event in ("complete", "error"):
                        break
            elapsed = round(time.perf_counter() - t0, 2)
            return {"user": user_idx, "status": resp.status, "elapsed_s": elapsed, "events": events_received, "last_event": last_event}
    except Exception as e:
        elapsed = round(time.perf_counter() - t0, 2)
        return {"user": user_idx, "status": "error", "elapsed_s": elapsed, "error": str(e)[:100]}


async def main(num_users: int, endpoint: str):
    print(f"\n{'='*60}")
    print(f"  Load Test: {num_users} concurrent users → /{endpoint}")
    print(f"{'='*60}\n")

    connector = aiohttp.TCPConnector(limit=num_users)
    async with aiohttp.ClientSession(connector=connector) as session:
        t0 = time.perf_counter()

        if endpoint == "simulate":
            tasks = [send_simulate(session, i) for i in range(num_users)]
        else:
            tasks = [send_stream(session, i) for i in range(num_users)]

        results = await asyncio.gather(*tasks)
        total_time = round(time.perf_counter() - t0, 2)

    # ── Summary ──────────────────────────────────────────────────
    successes = [r for r in results if r.get("status") == 200]
    errors = [r for r in results if r.get("status") != 200]
    elapsed_times = [r["elapsed_s"] for r in results]

    print(f"\n{'─'*60}")
    print(f"  Results")
    print(f"{'─'*60}")
    print(f"  Total time:    {total_time}s")
    print(f"  Succeeded:     {len(successes)}/{num_users}")
    print(f"  Failed:        {len(errors)}/{num_users}")
    if elapsed_times:
        print(f"  Fastest:       {min(elapsed_times)}s")
        print(f"  Slowest:       {max(elapsed_times)}s")
        print(f"  Avg:           {round(sum(elapsed_times)/len(elapsed_times), 2)}s")
    print(f"{'─'*60}\n")

    if errors:
        print("  Errors:")
        for e in errors[:5]:
            print(f"    user-{e['user']}: {e.get('error', e.get('status'))}")
        if len(errors) > 5:
            print(f"    ... and {len(errors)-5} more")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load test the simulation agent")
    parser.add_argument("--users", type=int, default=10, help="Number of concurrent users (default: 10)")
    parser.add_argument("--endpoint", choices=["simulate", "stream"], default="simulate", help="Which endpoint to test")
    args = parser.parse_args()

    asyncio.run(main(args.users, args.endpoint))
