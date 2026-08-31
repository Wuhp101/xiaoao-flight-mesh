from __future__ import annotations

import asyncio
import json
import os
import urllib.request

from .server import search_batch
from .planner import scan_page


def request_json(url: str, token: str, data: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "xiaoao-flight-mesh/0.1",
    }, method="GET" if data is None else "POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> None:
    job_url = os.environ["FLIGHT_MESH_JOB_URL"]
    token = os.environ["FLIGHT_MESH_TOKEN"]
    job = request_json(job_url, token)
    searches = job.get("searches", [])
    plan_state = None
    if not searches and isinstance(job.get("plan"), dict):
        plan_state = scan_page(
            job["plan"], cursor=int(job.get("cursor") or 0),
            limit=int(job.get("limit") or os.getenv("FLIGHT_MESH_MAX_SEARCHES", "12")),
            completed_keys=job.get("completedKeys") or [],
            priority_keys=job.get("priorityKeys") or [],
        )
        searches = plan_state["queries"]
    if not searches:
        print("No pending flight searches.")
        return
    result = asyncio.run(search_batch(searches))
    if plan_state:
        result["dateMatrix"] = {key: value for key, value in plan_state.items() if key != "queries"}
    ingest_url = job.get("ingestUrl")
    if not ingest_url:
        raise RuntimeError("job response did not include ingestUrl")
    accepted = request_json(ingest_url, token, result)
    print(json.dumps({"coverage": result["coverage"], "accepted": accepted.get("ok", False)}))


if __name__ == "__main__":
    main()
