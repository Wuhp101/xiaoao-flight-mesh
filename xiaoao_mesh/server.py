from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import clean_query
from .planner import scan_page
from .providers import FastFlightsProvider, GooglePlaywrightProvider, SerpApiGoogleFlightsProvider
from .quality import deduplicate_offers
from .snapshots import SnapshotStore


PROVIDER_HEALTH: dict[str, dict[str, Any]] = {}
SNAPSHOTS = SnapshotStore()


def configured_provider_names() -> list[str]:
    value = os.getenv("FLIGHT_MESH_PROVIDERS", "google-playwright,serpapi-google-flights,fast-flights")
    allowed = {"google-playwright", "serpapi-google-flights", "fast-flights"}
    return [name for name in dict.fromkeys(part.strip() for part in value.split(",")) if name in allowed]


def make_provider(name: str):
    if name == "google-playwright":
        timeout = max(5_000, min(120_000, int(os.getenv("FLIGHT_MESH_BROWSER_TIMEOUT_MS", "45000"))))
        pages = max(1, min(3, int(os.getenv("FLIGHT_MESH_BROWSER_PAGES", "2"))))
        return GooglePlaywrightProvider(timeout, pages)
    if name == "serpapi-google-flights":
        return SerpApiGoogleFlightsProvider()
    if name == "fast-flights":
        return FastFlightsProvider()
    raise ValueError(f"unsupported provider: {name}")


def provider_available(name: str) -> bool:
    if name == "serpapi-google-flights" and not os.getenv("SERPAPI_KEY"):
        return False
    health = PROVIDER_HEALTH.get(name, {})
    return float(health.get("openUntil") or 0) <= time.time()


def record_provider_health(name: str, ok: bool, elapsed_ms: int, error: str = "") -> None:
    previous = PROVIDER_HEALTH.get(name, {})
    attempts = int(previous.get("attempts") or 0) + 1
    successes = int(previous.get("successes") or 0) + (1 if ok else 0)
    failures = int(previous.get("failures") or 0) + (0 if ok else 1)
    consecutive = 0 if ok else int(previous.get("consecutiveFailures") or 0) + 1
    average = round((float(previous.get("averageMs") or 0) * (attempts - 1) + elapsed_ms) / attempts)
    PROVIDER_HEALTH[name] = {
        "attempts": attempts, "successes": successes, "failures": failures,
        "successRate": round(successes / attempts * 100, 1), "averageMs": average,
        "consecutiveFailures": consecutive, "lastError": "" if ok else error[:160],
        "openUntil": time.time() + 30 * 60 if consecutive >= 3 else 0,
    }


async def search_batch(searches: list[dict[str, Any]]) -> dict[str, Any]:
    limit = max(1, min(30, int(os.getenv("FLIGHT_MESH_MAX_SEARCHES", "12"))))
    cleaned = [clean_query(item) for item in searches[:limit]]
    provider_names = configured_provider_names()
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    providers: list[tuple[str, Any]] = []
    for name in provider_names:
        if not provider_available(name):
            failures.append({"provider": name, "query": "batch", "error": "not configured or circuit open"})
            continue
        try:
            provider = make_provider(name)
            start = getattr(provider, "start", None)
            if start:
                await start()
            providers.append((name, provider))
        except Exception as error:
            failures.append({"provider": name, "query": "batch", "error": str(error)[:240]})

    query_limit = max(1, min(6, int(os.getenv("FLIGHT_MESH_QUERY_CONCURRENCY", "2"))))
    query_semaphore = asyncio.Semaphore(query_limit)

    async def one_query(query: dict[str, Any]) -> dict[str, Any] | None:
      async with query_semaphore:
        combined: list[dict[str, Any]] = []
        sources: list[str] = []
        async def call_provider(name: str, provider: Any) -> None:
            started = time.perf_counter()
            try:
                offers = await provider.search(query)
                combined.extend(offers)
                if offers:
                    sources.append(name)
                record_provider_health(name, True, int((time.perf_counter() - started) * 1000))
            except Exception as error:  # Partial provider failure must not abort the batch.
                failures.append({"provider": name, "query": f"{query['origin']}-{query['destination']}", "error": str(error)[:240]})
                record_provider_health(name, False, int((time.perf_counter() - started) * 1000), str(error))

        await asyncio.gather(*(call_provider(name, provider) for name, provider in providers))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if combined:
            for offer in combined:
                offer["fetchedAt"] = now
                offer["verifiedAt"] = now
                offer["priceFreshness"] = "fresh"
            results = deduplicate_offers(query, combined)
            await asyncio.to_thread(SNAPSHOTS.put, query, results, now)
            return {"input": query, "provider": "+".join(sorted(set(sources))), "results": results, "snapshot": False}
        snapshot = await asyncio.to_thread(SNAPSHOTS.get, query)
        if snapshot:
            observed_at, cached = snapshot
            cached_results = [{**item, "priceFreshness": "snapshot", "snapshotObservedAt": observed_at,
                "verifiedAt": "", "verificationState": "snapshot"} for item in cached]
            return {"input": query, "provider": "last-known-good-snapshot", "results": cached_results, "snapshot": True}
        return None

    try:
        rows = await asyncio.gather(*(one_query(query) for query in cleaned))
        completed = [row for row in rows if row]
    finally:
        await asyncio.gather(*(provider.close() for _, provider in providers if getattr(provider, "close", None)), return_exceptions=True)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "ok": True,
        "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
        "providers": provider_names,
        "fetchedAt": now,
        "searches": completed,
        "coverage": {"requested": len(cleaned), "completed": len(completed), "failed": len(cleaned) - len(completed)},
        "providerHealth": PROVIDER_HEALTH,
        "snapshotsUsed": sum(1 for item in completed if item.get("snapshot")),
        "failures": failures[:30],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoaoFlightMesh/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"flight-mesh {self.address_string()} {format % args}")

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authenticated(self) -> bool:
        configured = os.getenv("FLIGHT_MESH_TOKEN", "")
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return len(configured) >= 32 and hmac.compare_digest(configured, supplied)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, {
            "ok": True,
            "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
            "providers": configured_provider_names(),
            "authenticated": len(os.getenv("FLIGHT_MESH_TOKEN", "")) >= 32,
            "plannerVersion": "date-matrix-v1",
            "providerHealth": PROVIDER_HEALTH,
        })

    def do_POST(self) -> None:
        if self.path not in {"/search-batch", "/plan"}:
            self.send_json(404, {"error": "not found"})
            return
        if not self.authenticated():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = min(1_000_000, int(self.headers.get("Content-Length", "0")))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/plan":
                plan = payload.get("plan", payload)
                page = scan_page(
                    plan, cursor=int(payload.get("cursor") or 0),
                    limit=int(payload.get("limit") or os.getenv("FLIGHT_MESH_MAX_SEARCHES", "12")),
                    completed_keys=payload.get("completedKeys") or [],
                    priority_keys=payload.get("priorityKeys") or [],
                )
                self.send_json(200, {"ok": True, **page})
                return
            searches = payload.get("searches", [])
            if not isinstance(searches, list):
                raise ValueError("searches must be an array")
            self.send_json(200, asyncio.run(search_batch(searches)))
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(502, {"error": str(error)[:300]})


def main() -> None:
    host = os.getenv("FLIGHT_MESH_HOST", "0.0.0.0")
    port = int(os.getenv("FLIGHT_MESH_PORT", "8789"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"flight-mesh v1 listening on {host}:{port}; providers={','.join(configured_provider_names())}")
    server.serve_forever()


if __name__ == "__main__":
    main()
