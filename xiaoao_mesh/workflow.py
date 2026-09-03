from __future__ import annotations

import asyncio
import json
import os
import urllib.request

from .core import clean_query
from .planner import query_key, scan_page
from .recovery import search_fast_recovery_batch
from .server import search_batch, search_fast_batch, utc_now


POPULAR_DESTINATIONS = (
    "NRT", "HND", "KIX", "ICN", "GMP", "BKK", "DMK", "SIN", "TPE", "KHH",
    "FUK", "CTS", "OKA", "PUS", "CJU", "MNL", "CEB", "KUL", "DPS", "CGK",
)


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
    found = {
        query_key(row.get("input") or {})
        for row in fast_result.get("searches", [])
        if row.get("input") and row.get("results")
    }
    requested = int((fast_result.get("coverage") or {}).get("requested") or len(searches))
    missing = []
    for raw in searches[:requested]:
        query = clean_query(raw)
        if query_key(query) in found:
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
    positions = {query_key(row.get("input") or {}): index for index, row in enumerate(rows) if row.get("input")}
    for row in recovery.get("searches") or []:
        key = query_key(row.get("input") or {})
        if key not in positions:
            positions[key] = len(rows)
            rows.append(row)
            continue
        previous = rows[positions[key]]
        if row.get("results") and not previous.get("results"):
            rows[positions[key]] = row
    return {**primary, "searches": rows}


def popular_miss_searches(searches: list[dict], discovery_result: dict, limit: int) -> list[dict]:
    """Reserve full Google checks for popular routes that fast sources missed."""
    outcomes = {
        query_key(row.get("input") or {}): row
        for row in discovery_result.get("searches", []) if row.get("input")
    }
    candidates = []
    for raw in searches:
        query = clean_query(raw)
        if query.get("destination") not in POPULAR_DESTINATIONS:
            continue
        row = outcomes.get(query_key(query), {})
        if row.get("results"):
            continue
        candidates.append(query)
    candidates.sort(key=lambda query: (POPULAR_DESTINATIONS.index(query["destination"]), query["origin"], query["cabin"]))
    selected = []
    seen_destinations = set()
    for query in candidates:
        if query["destination"] in seen_destinations:
            continue
        selected.append(query)
        seen_destinations.add(query["destination"])
        if len(selected) >= limit:
            break
    return selected


def verification_searches(searches: list[dict], discovery_result: dict, limit: int, popular_limit: int) -> list[dict]:
    selected = popular_miss_searches(searches, discovery_result, min(limit, popular_limit))
    seen = {query_key(query) for query in selected}
    for query in shortlist_searches(discovery_result, limit):
        if query_key(query) in seen:
            continue
        selected.append(query)
        seen.add(query_key(query))
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

    # Collect every phase locally and ingest the page once. Cloudflare KV is
    # eventually consistent, so three rapid writes for the same job can read
    # stale state and make the cursor move backwards.
    fast_result = asyncio.run(search_fast_batch(searches))
    fast_result["discoveryWave"] = 1
    if plan_state:
        fast_result["dateMatrix"] = {key: value for key, value in plan_state.items() if key != "queries"}

    # Wave 2: only first-wave misses, browserless Google Shopping RPC. This can
    # be slower on some routes, but it happens after Wave 1 is already visible.
    recovery_limit = max(0, min(24, int(os.getenv("FLIGHT_MESH_RPC_RECOVERY_MAX_SEARCHES", "12"))))
    recovery_inputs = recovery_searches(searches, fast_result, recovery_limit) if recovery_limit else []
    recovery_result = None
    if recovery_inputs:
        recovery_result = asyncio.run(search_fast_recovery_batch(recovery_inputs))
        recovery_result["phaseOf"] = "fast-discovery"
        recovery_result["discoveryWave"] = 2

    discovery_result = merge_discovery(fast_result, recovery_result)

    # Wave 3: verify only the best candidates. Browser/API verification never
    # blocks the first two discovery waves from appearing. Even when discovery
    # found no priced candidate, send an empty verified phase so the final page
    # can transition from `verifying` to `completed` instead of hanging forever.
    verify_limit = max(1, min(12, int(os.getenv("FLIGHT_MESH_VERIFY_CANDIDATES", "10"))))
    popular_limit = max(0, min(8, int(os.getenv("FLIGHT_MESH_POPULAR_RECOVERY_SEARCHES", "4"))))
    verify_searches = verification_searches(searches, discovery_result, verify_limit, popular_limit)
    if verify_searches:
        verified_result = asyncio.run(search_batch(verify_searches))
        verified_result["phaseOf"] = "fast-discovery"
        verified_result["discoveryWave"] = 3
    else:
        verified_result = {
            "ok": True,
            "mode": "verified",
            "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
            "providers": [],
            "fetchedAt": utc_now(),
            "searches": [],
            "coverage": {"requested": 0, "processed": 0, "completed": 0, "found": 0, "snapshots": 0, "noResults": 0, "failed": 0},
            "providerHealth": {},
            "snapshotsUsed": 0,
            "failures": [],
            "phaseOf": "fast-discovery",
            "discoveryWave": 3,
            "emptyTerminal": True,
        }
    if plan_state:
        date_matrix = {key: value for key, value in plan_state.items() if key != "queries"}
    else:
        date_matrix = {
            "cursor": 0,
            "nextCursor": None,
            "matrixTotal": len(searches),
            "completed": 0,
            "remaining": 0,
            "datePairs": 1,
        }
    phases = [fast_result]
    if recovery_result:
        phases.append(recovery_result)
    phases.append(verified_result)
    page_result = {
        "ok": True,
        "mode": "page-batch",
        "fetchedAt": verified_result.get("fetchedAt") or fast_result.get("fetchedAt"),
        "coverage": fast_result.get("coverage") or {},
        "dateMatrix": date_matrix,
        "providerHealth": verified_result.get("providerHealth") or fast_result.get("providerHealth") or {},
        "phases": phases,
    }
    page_accepted = request_json(ingest_url, token, page_result)

    print(json.dumps({
        "fastCoverage": fast_result["coverage"],
        "recoveryRequested": len(recovery_inputs),
        "recoveryCoverage": recovery_result.get("coverage") if recovery_result else None,
        "verifiedRequested": len(verify_searches),
        "verifiedCoverage": verified_result.get("coverage"),
        "pageAccepted": page_accepted.get("ok", False),
    }))


if __name__ == "__main__":
    main()
