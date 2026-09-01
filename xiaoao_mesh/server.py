from __future__ import annotations

import asyncio
import hashlib
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
        pages = max(1, min(6, int(os.getenv("FLIGHT_MESH_BROWSER_PAGES", "3"))))
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
        "attempts": attempts,
        "successes": successes,
        "failures": failures,
        "successRate": round(successes / attempts * 100, 1),
        "averageMs": average,
        "consecutiveFailures": consecutive,
        "lastError": "" if ok else error[:160],
        "openUntil": time.time() + 30 * 60 if consecutive >= 3 else 0,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bridge_token_hash() -> str:
    """Return a non-secret identifier for the NAS-local bearer token.

    The runtime token is 256 bits of random data generated on the NAS. Exposing
    only SHA-256(token) lets the Cloudflare bridge verify the token without ever
    copying the bearer secret into GitHub or source control.
    """
    token = os.getenv("FLIGHT_MESH_TOKEN", "")
    if len(token) < 32:
        return ""
    return hashlib.sha256(token.encode()).hexdigest()


async def search_fast_batch(searches: list[dict[str, Any]]) -> dict[str, Any]:
    """High-throughput discovery path.

    This path intentionally uses only the non-browser fast-flights adapter and
    marks every result as an unverified candidate. It must never trigger a god
    price alert by itself.
    """
    limit = max(1, min(120, int(os.getenv("FLIGHT_MESH_FAST_MAX_SEARCHES", "60"))))
    cleaned = [clean_query(item) for item in searches[:limit]]
    concurrency = max(2, min(32, int(os.getenv("FLIGHT_MESH_FAST_CONCURRENCY", "12"))))
    semaphore = asyncio.Semaphore(concurrency)
    failures: list[dict[str, str]] = []
    provider = make_provider("fast-flights")

    async def one_query(query: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            started = time.perf_counter()
            try:
                offers = await provider.search(query)
                record_provider_health("fast-flights", True, int((time.perf_counter() - started) * 1000))
            except Exception as error:
                record_provider_health("fast-flights", False, int((time.perf_counter() - started) * 1000), str(error))
                failures.append({
                    "provider": "fast-flights",
                    "query": f"{query['origin']}-{query['destination']}",
                    "error": str(error)[:240],
                })
                return None
            if not offers:
                return None
            fetched_at = utc_now()
            candidates = []
            for offer in offers:
                candidates.append({
                    **offer,
                    "fetchedAt": fetched_at,
                    "verifiedAt": "",
                    "priceFreshness": "fresh-candidate",
                    "verificationState": "candidate",
                    "candidate": True,
                    "godPriceEligible": False,
                })
            candidates.sort(key=lambda item: float(item.get("price") or 10**18))
            return {
                "input": query,
                "provider": "fast-flights",
                "results": candidates[:8],
                "snapshot": False,
                "verificationPending": True,
            }

    rows = await asyncio.gather(*(one_query(query) for query in cleaned))
    completed = [row for row in rows if row]
    return {
        "ok": True,
        "mode": "fast-discovery",
        "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
        "providers": ["fast-flights"],
        "fetchedAt": utc_now(),
        "searches": completed,
        "coverage": {
            "requested": len(cleaned),
            "completed": len(completed),
            "failed": len(cleaned) - len(completed),
        },
        "providerHealth": PROVIDER_HEALTH,
        "snapshotsUsed": 0,
        "failures": failures[:60],
    }


async def search_batch(
    searches: list[dict[str, Any]], provider_names: list[str] | None = None
) -> dict[str, Any]:
    """Verified search path.

    Slow browser/API providers belong here. Callers should normally pass only
    shortlisted candidates instead of the whole date matrix.
    """
    limit = max(1, min(30, int(os.getenv("FLIGHT_MESH_MAX_SEARCHES", "12"))))
    cleaned = [clean_query(item) for item in searches[:limit]]
    names = provider_names or configured_provider_names()
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    providers: list[tuple[str, Any]] = []
    for name in names:
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

    query_limit = max(1, min(12, int(os.getenv("FLIGHT_MESH_QUERY_CONCURRENCY", "4"))))
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
                except Exception as error:
                    failures.append({
                        "provider": name,
                        "query": f"{query['origin']}-{query['destination']}",
                        "error": str(error)[:240],
                    })
                    record_provider_health(name, False, int((time.perf_counter() - started) * 1000), str(error))

            await asyncio.gather(*(call_provider(name, provider) for name, provider in providers))
            fetched_at = utc_now()
            if combined:
                for offer in combined:
                    offer["fetchedAt"] = fetched_at
                    offer["verifiedAt"] = fetched_at
                    offer["priceFreshness"] = "fresh"
                    offer["candidate"] = False
                results = deduplicate_offers(query, combined)
                await asyncio.to_thread(SNAPSHOTS.put, query, results, fetched_at)
                return {
                    "input": query,
                    "provider": "+".join(sorted(set(sources))),
                    "results": results,
                    "snapshot": False,
                    "verificationPending": False,
                }
            snapshot = await asyncio.to_thread(SNAPSHOTS.get, query)
            if snapshot:
                observed_at, cached = snapshot
                cached_results = [{
                    **item,
                    "priceFreshness": "snapshot",
                    "snapshotObservedAt": observed_at,
                    "verifiedAt": "",
                    "verificationState": "snapshot",
                    "candidate": False,
                } for item in cached]
                return {
                    "input": query,
                    "provider": "last-known-good-snapshot",
                    "results": cached_results,
                    "snapshot": True,
                    "verificationPending": True,
                }
            return None

    try:
        rows = await asyncio.gather(*(one_query(query) for query in cleaned))
        completed = [row for row in rows if row]
    finally:
        await asyncio.gather(*(
            provider.close() for _, provider in providers if getattr(provider, "close", None)
        ), return_exceptions=True)
    return {
        "ok": True,
        "mode": "verified",
        "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
        "providers": names,
        "fetchedAt": utc_now(),
        "searches": completed,
        "coverage": {
            "requested": len(cleaned),
            "completed": len(completed),
            "failed": len(cleaned) - len(completed),
        },
        "providerHealth": PROVIDER_HEALTH,
        "snapshotsUsed": sum(1 for item in completed if item.get("snapshot")),
        "failures": failures[:30],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoaoFlightMesh/2.0"

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
            "bridgeTokenHash": bridge_token_hash(),
            "plannerVersion": "date-matrix-v1",
            "speedEngineVersion": "v2",
            "fastConcurrency": max(2, min(32, int(os.getenv("FLIGHT_MESH_FAST_CONCURRENCY", "12")))),
            "providerHealth": PROVIDER_HEALTH,
        })

    def do_POST(self) -> None:
        if self.path not in {"/search-batch", "/search-fast", "/plan"}:
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
                    plan,
                    cursor=int(payload.get("cursor") or 0),
                    limit=int(payload.get("limit") or os.getenv("FLIGHT_MESH_FAST_MAX_SEARCHES", "60")),
                    completed_keys=payload.get("completedKeys") or [],
                    priority_keys=payload.get("priorityKeys") or [],
                )
                self.send_json(200, {"ok": True, **page})
                return
            searches = payload.get("searches", [])
            if not isinstance(searches, list):
                raise ValueError("searches must be an array")
            if self.path == "/search-fast":
                self.send_json(200, asyncio.run(search_fast_batch(searches)))
            else:
                self.send_json(200, asyncio.run(search_batch(searches)))
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(502, {"error": str(error)[:300]})


def main() -> None:
    host = os.getenv("FLIGHT_MESH_HOST", "0.0.0.0")
    port = int(os.getenv("FLIGHT_MESH_PORT", "8789"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"flight-mesh v2 listening on {host}:{port}; providers={','.join(configured_provider_names())}")
    server.serve_forever()


if __name__ == "__main__":
    main()
