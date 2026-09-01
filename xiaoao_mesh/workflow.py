from __future__ import annotations

import asyncio
import json
import os
import urllib.request

from .planner import scan_page
from .server import search_batch, search_fast_batch


def request_json(url: str, token: str, data: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "xiaoao-flight-mesh/2.0",
    }, method="GET" if data is None else "POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def shortlist_searches(fast_result: dict, limit: int) -> list[dict]:
    ranked = []
    for row in fast_result.get("searches", []):
        prices = [float(item.get("price")) for item in row.get("results", []) if item.get("price")]
        if prices:
            ranked.append((min(prices), row.get("input", {})))
    ranked.sort(key=lambda item: item[0])
    selected = []
    seen_destinations = set()
    for _, query in ranked:
        destination = str(query.get("destination") or "")
        if destination and destination not in seen_destinations:
            selected.append(query)
            seen_destinations.add(destination)
            if len(selected) >= limit:
                return selected
    for _, query in ranked:
        if query in selected:
            continue
        selected.append(query)
        if len(selected) >= limit:
            break
    return selected


def main() -> None:
    job_url = os.environ["FLIGHT_MESH_JOB_URL"]
    token = os.environ["FLIGHT_MESH_TOKEN"]
    job = request_json(job_url, token)
    searches = job.get("searches", [])
    plan_state = None
    if not searches and isinstance(job.get("plan"), dict):
        plan_state = scan_page(
            job["plan"], cursor=int(job.get("cursor") or 0),
            limit=int(job.get("limit") or os.getenv("FLIGHT_MESH_FAST_MAX_SEARCHES", "60")),
            completed_keys=job.get("completedKeys") or [],
            priority_keys=job.get("priorityKeys") or [],
        )
        searches = plan_state["queries"]
    if not searches:
        print("No pending flight searches.")
        return

    ingest_url = job.get("ingestUrl")
    if not ingest_url:
        raise RuntimeError("job response did not include ingestUrl")

    fast_result = asyncio.run(search_fast_batch(searches))
    if plan_state:
        fast_result["dateMatrix"] = {key: value for key, value in plan_state.items() if key != "queries"}
    fast_accepted = request_json(ingest_url, token, fast_result)

    verify_limit = max(1, min(12, int(os.getenv("FLIGHT_MESH_VERIFY_CANDIDATES", "6"))))
    verify_searches = shortlist_searches(fast_result, verify_limit)
    verified_accepted = {"ok": True}
    verified_result = None
    if verify_searches:
        verified_result = asyncio.run(search_batch(verify_searches))
        verified_result["phaseOf"] = "fast-discovery"
        verified_accepted = request_json(ingest_url, token, verified_result)

    print(json.dumps({
        "fastCoverage": fast_result["coverage"],
        "fastAccepted": fast_accepted.get("ok", False),
        "verifiedRequested": len(verify_searches),
        "verifiedCoverage": verified_result.get("coverage") if verified_result else None,
        "verifiedAccepted": verified_accepted.get("ok", False),
    }))


if __name__ == "__main__":
    main()
