from __future__ import annotations

import asyncio
import os
from typing import Any

from .core import clean_query
from .providers import FastFlightsProvider
from .server import search_coverage, utc_now


def _candidate_row(query: dict[str, Any], offers: list[dict[str, Any]]) -> dict[str, Any]:
    if not offers:
        return {
            "input": query,
            "provider": "faster-flights-shopping-rpc",
            "results": [],
            "outcome": "no-results",
            "outcomeReason": "Google Shopping 快速補查已完成，但沒有回傳可讀價格",
            "snapshot": False,
            "verificationPending": True,
        }
    fetched_at = utc_now()
    candidates = [{
        **offer,
        "fetchedAt": fetched_at,
        "verifiedAt": "",
        "priceFreshness": "fresh-candidate",
        "verificationState": "candidate",
        "candidate": True,
        "godPriceEligible": False,
        "discoveryWave": "shopping-rpc-recovery",
    } for offer in offers]
    candidates.sort(key=lambda item: float(item.get("price") or 10**18))
    return {
        "input": query,
        "provider": "faster-flights-shopping-rpc",
        "results": candidates[:8],
        "outcome": "found",
        "snapshot": False,
        "verificationPending": True,
    }


async def search_fast_recovery_batch(searches: list[dict[str, Any]]) -> dict[str, Any]:
    """Recover first-wave misses through Google's browserless shopping RPC.

    This function is deliberately called only *after* first-pass candidates
    have already been ingested, so slow RPC routes never hold the first screen.
    """
    limit = max(1, min(24, int(os.getenv("FLIGHT_MESH_RPC_RECOVERY_MAX_SEARCHES", "12"))))
    concurrency = max(1, min(12, int(os.getenv("FLIGHT_MESH_RPC_RECOVERY_CONCURRENCY", "6"))))
    cleaned = [clean_query(item) for item in searches[:limit]]
    semaphore = asyncio.Semaphore(concurrency)
    provider = FastFlightsProvider()
    failures: list[dict[str, str]] = []

    async def one_query(query: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                offers = await provider.search_shopping(query)
                return _candidate_row(query, offers)
            except Exception as error:
                failures.append({
                    "provider": "faster-flights-shopping-rpc",
                    "query": f"{query['origin']}-{query['destination']}",
                    "error": str(error)[:240],
                })
                return {
                    "input": query,
                    "provider": "faster-flights-shopping-rpc",
                    "results": [],
                    "outcome": "source-failed",
                    "outcomeReason": str(error)[:240],
                    "snapshot": False,
                    "verificationPending": True,
                }

    rows = await asyncio.gather(*(one_query(query) for query in cleaned))
    return {
        "ok": True,
        "mode": "fast-recovery",
        "node": os.getenv("FLIGHT_MESH_NODE", "nas"),
        "providers": ["faster-flights-shopping-rpc"],
        "fetchedAt": utc_now(),
        "searches": rows,
        "coverage": search_coverage(rows),
        "snapshotsUsed": 0,
        "failures": failures[:24],
    }
