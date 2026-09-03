from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable


TRUSTED_LIVE_PROVIDERS = {"duffel", "amadeus", "serpapi-google-flights"}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def comparable_family_price(offer: dict[str, Any], passenger_count: int) -> float | None:
    price = _number(offer.get("price"))
    if price is None:
        return None
    if offer.get("priceScope") != "family":
        return None
    if int(offer.get("passengerCount") or 0) != passenger_count:
        return None
    if offer.get("taxIncluded") is not True:
        return None
    return price


def offer_identity(query: dict[str, Any], offer: dict[str, Any]) -> str:
    airline = str(offer.get("airlineCode") or offer.get("airline") or "unknown").lower().replace(" ", "")
    departure = str(offer.get("departureTime") or "")
    return "|".join(str(value).lower() for value in (
        query.get("origin"), query.get("destination"), query.get("outboundDate"),
        query.get("returnDate"), query.get("cabin"), airline, departure,
    ))


def deduplicate_offers(query: dict[str, Any], offers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    passenger_count = int(query.get("adults") or 1) + int(query.get("children") or 0)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in offers:
        offer = dict(raw)
        grouped[offer_identity(query, offer)].append(offer)
    output: list[dict[str, Any]] = []
    for identity, group in grouped.items():
        prices = [(comparable_family_price(item, passenger_count), item) for item in group]
        valid = [(price, item) for price, item in prices if price is not None]
        selected = min(valid, key=lambda pair: pair[0])[1] if valid else group[0]
        comparable = [price for price, _ in valid]
        providers = sorted(set(str(item.get("provider") or item.get("source") or "unknown") for item in group))
        verification_providers = sorted(set(
            str(item.get("provider") or item.get("source") or "unknown")
            for price, item in prices if price is not None
        ))
        spread = 0.0
        if len(comparable) > 1 and min(comparable) > 0:
            spread = (max(comparable) - min(comparable)) / min(comparable)
        market = next((item for item in group if item.get("marketPriceSource") == "google-price-insights"), {})
        output.append({
            **selected,
            **{key: market.get(key) for key in (
                "marketPriceLow", "marketPriceHigh", "marketPriceLevel", "marketPriceSource"
            ) if market.get(key) not in (None, "")},
            "identity": identity,
            "supportingProviders": providers,
            "verificationProviders": verification_providers,
            "independentSourceCount": len(verification_providers),
            "sourcePriceSpreadPct": round(spread * 100, 2),
            "sourceConflict": spread > 0.08,
            "verificationState": "conflict" if spread > 0.08 else "cross-checked" if len(verification_providers) > 1 else "reference",
        })
    return sorted(output, key=lambda item: comparable_family_price(item, passenger_count) or math.inf)


def percentile(values: Iterable[float], ratio: float) -> float | None:
    ordered = sorted(float(value) for value in values if _number(value) is not None)
    if not ordered:
        return None
    position = (len(ordered) - 1) * max(0.0, min(1.0, ratio))
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def classify_price_opportunity(
    offer: dict[str, Any], history: Iterable[float], *, passenger_count: int,
    max_family_price: float | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    price = comparable_family_price(offer, passenger_count)
    history_values = [value for value in (_number(item) for item in history) if value is not None]
    ceiling = _number(max_family_price)
    within_budget = price is not None and (ceiling is None or price <= ceiling)
    p10 = percentile(history_values, 0.10)
    median = statistics.median(history_values) if history_values else None
    statistically_low = bool(price is not None and len(history_values) >= 5 and (
        (p10 is not None and price <= p10) or (median is not None and price <= median * 0.75)
    ))
    provider = str(offer.get("provider") or offer.get("source") or "")
    sources = int(offer.get("independentSourceCount") or 0)
    source_conflict = offer.get("sourceConflict") is True
    bookable = offer.get("bookable") is True and provider in TRUSTED_LIVE_PROVIDERS
    checked_at = str(offer.get("verifiedAt") or offer.get("fetchedAt") or "")
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        fresh = 0 <= (current - checked).total_seconds() <= 15 * 60
    except ValueError:
        fresh = False
    verified = price is not None and fresh and not source_conflict and (sources >= 2 or bookable)
    return {
        "comparable": price is not None,
        "withinBudget": within_budget,
        "statisticallyLow": statistically_low,
        "verified": verified,
        "godPrice": bool(within_budget and statistically_low and verified),
        "reason": "verified-historical-low" if within_budget and statistically_low and verified
            else "needs-second-source" if price is not None and statistically_low and not verified
            else "insufficient-history" if len(history_values) < 5
            else "not-a-historical-low",
        "historyCount": len(history_values),
        "historicalMedian": median,
        "historicalP10": p10,
    }
