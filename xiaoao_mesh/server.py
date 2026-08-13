from __future__ import annotations

import asyncio
import hmac
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import clean_query
from .providers import FastFlightsProvider, MetaSearchProvider


def configured_provider_names() -> list[str]:
    value = os.getenv("FLIGHT_MESH_PROVIDERS", "fast-flights,skyscanner,trip,kayak,expedia")
    allowed = {"fast-flights", "skyscanner", "trip", "kayak", "expedia"}
    return [name for name in dict.fromkeys(part.strip() for part in value.split(",")) if name in allowed]


def make_provider(name: str):
    if name == "fast-flights":
        return FastFlightsProvider()
    timeout = max(5_000, min(120_000, int(os.getenv("FLIGHT_MESH_BROWSER_TIMEOUT_MS", "45000"))))
    return MetaSearchProvider(name, timeout)


async def search_batch(searches: list[dict[str, Any]]) -> dict[str, Any]:
    limit = max(1, min(30, int(os.getenv("FLIGHT_MESH_MAX_SEARCHES", "12"))))
    cleaned = [clean_query(item) for item in searches[:limit]]
    provider_names = configured_provider_names()
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for query in cleaned:
        combined: list[dict[str, Any]] = []
        sources: list[str] = []
        for name in provider_names:
            try:
                offers = await make_provider(name).search(query)
                combined.extend(offers)
                if offers:
                    sources.append(name)
            except Exception as error:  # Partial provider failure must not abort the batch.
                failures.append({"provider": name, "query": f"{query['origin']}-{query['destination']}", "error": str(error)[:240]})
        if combined:
            completed.append({"input": query, "provider": "+".join(sources), "results": combined})
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "ok": True,
        "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
        "providers": provider_names,
        "fetchedAt": now,
        "searches": completed,
        "coverage": {"requested": len(cleaned), "completed": len(completed), "failed": len(cleaned) - len(completed)},
        "failures": failures[:30],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoaoFlightMesh/0.1"

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
        })

    def do_POST(self) -> None:
        if self.path != "/search-batch":
            self.send_json(404, {"error": "not found"})
            return
        if not self.authenticated():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = min(1_000_000, int(self.headers.get("Content-Length", "0")))
            payload = json.loads(self.rfile.read(length) or b"{}")
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
    print(f"flight-mesh listening on {host}:{port}; providers={','.join(configured_provider_names())}")
    server.serve_forever()


if __name__ == "__main__":
    main()
