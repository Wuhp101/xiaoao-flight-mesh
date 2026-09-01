from __future__ import annotations

import asyncio
import json
import os
import urllib.request

from .core import clean_query
from .planner import query_key, scan_page
from .recovery import search_fast_recovery_batch
from .server import search_batch, search_fast_batch


def request_json(url: str, token: str, data: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "xiaoao-flight-mesh/2.1",
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


def recovery_searches(searches: list[dict], fast_result: dict, limit: int) -> list[dict]:
    """Pick first-wave misses while spreading RPC recovery across destinations."""
    completed = {
        query_key(row.get("input") or {})
        for row in fast_result.get("searches", [])
        if row.get("input")
    }
    requested = int((fast_result.get("coverage") or {}).get("requested") or len(searches))
    missing = []
    for raw in searches[:requested]:
        query = clean_query(raw)
        if query_key(query) in completed:
            continue
        # The fast library no longer exposes a checked-bag filter. Do not use
        # unverified hints when the user explicitly requires baggage.
        if int(query.get("checkedBags") or 0) > 0:
            continue
        missing.append(query)

    selected = []
    seen_destinations = set()
    for query in missing:
        destination = str(query.get("destination") or "")
        if destination and destination not in seen_destinations:
            selected.append(query)
            seen_destinations.add(destination)
            if len(selected) >= limit:
                return selected
    for query in missing:
        if query in selected:
            continue
        selected.append(query)
        if len(selected) >= limit:
            break
    return selected


def merge_discovery(primary: dict, recovery: dict | None) -> dict:
    if not recovery:
        return primary
    rows = list(primary.get("searches") or [])
    seen = {query_key(row.get("input") or {}) for row in rows if row.get("input")}
    for row in recovery.get("searches") or []:
        key = query_key(row.get("input") or {})
        if key not in seen:
            rows.append(row)
            seen.add(key)
    return {**primary, "searches": rows}


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

    # Wave 1: sub-second / low-second discovery. Ingest immediately so the UI
    # can show useful candidates before any slower recovery or verification.
    fast_result = asyncio.run(search_fast_batch(searches))
    fast_result["discoveryWave"] = 1
    if plan_state:
        fast_result["dateMatrix"] = {key: value for key, value in plan_state.items() if key != "queries"}
    fast_accepted = request_json(ingest_url, token, fast_result)

    # Wave 2: only first-wave misses, browserless Google Shopping RPC. This can
    # be slower on some routes, but it happens after Wave 1 is already visible.
    recovery_limit = max(0, min(24, int(os.getenv("FLIGHT_MESH_RPC_RECOVERY_MAX_SEARCHES", "12"))))
    recovery_inputs = recovery_searches(searches, fast_result, recovery_limit) if recovery_limit else []
    recovery_result = None
    recovery_accepted = {"ok": True}
    if recovery_inputs:
        recovery_result = asyncio.run(search_fast_recovery_batch(recovery_inputs))
        recovery_result["phaseOf"] = "fast-discovery"
        recovery_result["discoveryWave"] = 2
        if recovery_result.get("searches"):
            recovery_accepted = request_json(ingest_url, token, recovery_result)

    discovery_result = merge_discovery(fast_result, recovery_result)

    # Wave 3: verify only the best candidates. Browser/API verification never
    # blocks the first two discovery waves from appearing.
    verify_limit = max(1, min(12, int(os.getenv("FLIGHT_MESH_VERIFY_CANDIDATES", "6"))))
    verify_searches = shortlist_searches(discovery_result, verify_limit)
    verified_accepted = {"ok": True}
    verified_result = None
    if verify_searches:
        verified_result = asyncio.run(search_batch(verify_searches))
        verified_result["phaseOf"] = "fast-discovery"
        verified_result["discoveryWave"] = 3
        verified_accepted = request_json(ingest_url, token, verified_result)

    print(json.dumps({
        "fastCoverage": fast_result["coverage"],
        "fastAccepted": fast_accepted.get("ok", False),
        "recoveryRequested": len(recovery_inputs),
        "recoveryCoverage": recovery_result.get("coverage") if recovery_result else None,
        "recoveryAccepted": recovery_accepted.get("ok", False),
        "verifiedRequested": len(verify_searches),
        "verifiedCoverage": verified_result.get("coverage") if verified_result else None,
        "verifiedAccepted": verified_accepted.get("ok", False),
    }))


if __name__ == "__main__":
    main()
