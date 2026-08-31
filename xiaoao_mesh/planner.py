from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product
from typing import Any, Iterable


@dataclass(frozen=True)
class DatePair:
    outbound: str
    returning: str
    days: int


def _iso(value: str) -> date:
    return date.fromisoformat(str(value))


def date_pairs(holiday_start: str, holiday_end: str, min_days: int, max_days: int) -> list[DatePair]:
    """Return every valid trip inside a holiday window.

    ``days`` intentionally matches the existing Xiaoao contract: it is the
    difference between the two calendar dates, not an inclusive day count.
    """

    start, end = _iso(holiday_start), _iso(holiday_end)
    if end <= start:
        raise ValueError("holidayEnd must be after holidayStart")
    available = (end - start).days
    minimum = max(2, min(int(min_days), available))
    maximum = max(minimum, min(int(max_days), available))
    output: list[DatePair] = []
    for duration in range(minimum, maximum + 1):
        departure = start
        while departure + timedelta(days=duration) <= end:
            returning = departure + timedelta(days=duration)
            output.append(DatePair(departure.isoformat(), returning.isoformat(), duration))
            departure += timedelta(days=1)
    return output


def _spread_order(values: list[DatePair]) -> list[DatePair]:
    """Visit centre and edges early while still guaranteeing full coverage."""

    if len(values) < 3:
        return values
    ordered: list[DatePair] = []
    left, right = 0, len(values) - 1
    middle = len(values) // 2
    ordered.append(values[middle])
    seen = {middle}
    while len(ordered) < len(values):
        for index in (left, right):
            if index not in seen:
                ordered.append(values[index])
                seen.add(index)
        left += 1
        right -= 1
    return ordered


def query_key(query: dict[str, Any]) -> str:
    return "|".join(str(query.get(name, "")).upper() for name in (
        "origin", "destination", "outboundDate", "returnDate", "cabin"
    )).lower()


def build_scan_matrix(plan: dict[str, Any]) -> list[dict[str, Any]]:
    origins = list(dict.fromkeys(str(value).upper() for value in plan.get("origins", []) if value))
    destinations = list(dict.fromkeys(str(value).upper() for value in plan.get("destinations", []) if value))
    cabins = list(dict.fromkeys(str(value) for value in plan.get("cabins", []) if value))
    if not origins or not destinations or not cabins:
        raise ValueError("origins, destinations and cabins are required")
    pairs = _spread_order(date_pairs(
        str(plan.get("holidayStart", "")), str(plan.get("holidayEnd", "")),
        int(plan.get("minDays") or 5), int(plan.get("maxDays") or 18),
    ))
    base = {
        "adults": max(1, min(4, int(plan.get("adults") or 2))),
        "children": max(0, min(3, int(plan.get("children") or 1))),
        "checkedBags": max(0, min(2, int(plan.get("checkedBags") or 0))),
        "currency": "HKD",
    }
    # Route-first interleaving makes the first sweep honest: every destination
    # receives one date observation before the second date pair is attempted.
    return [{
        **base,
        "origin": origin,
        "destination": destination,
        "outboundDate": pair.outbound,
        "returnDate": pair.returning,
        "cabin": cabin,
        "matrixDays": pair.days,
    } for pair, destination, origin, cabin in product(pairs, destinations, origins, cabins)]


def scan_page(
    plan: dict[str, Any], *, cursor: int = 0, limit: int = 12,
    completed_keys: Iterable[str] = (), priority_keys: Iterable[str] = (),
) -> dict[str, Any]:
    matrix = build_scan_matrix(plan)
    completed = {str(value).lower() for value in completed_keys}
    priority = {str(value).lower() for value in priority_keys}
    indexed = [(index, query) for index, query in enumerate(matrix) if query_key(query) not in completed]
    indexed.sort(key=lambda item: (query_key(item[1]) not in priority, item[0]))
    remaining = [query for _, query in indexed]
    start = max(0, int(cursor))
    size = max(1, min(100, int(limit)))
    page = remaining[start:start + size]
    total = len(matrix)
    return {
        "queries": page,
        "cursor": start,
        "nextCursor": start + len(page) if start + len(page) < len(remaining) else None,
        "matrixTotal": total,
        "completed": total - len(remaining),
        "remaining": max(0, len(remaining) - start - len(page)),
        "datePairs": len(date_pairs(
            str(plan.get("holidayStart", "")), str(plan.get("holidayEnd", "")),
            int(plan.get("minDays") or 5), int(plan.get("maxDays") or 18),
        )),
    }
