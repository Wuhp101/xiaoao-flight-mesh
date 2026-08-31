from __future__ import annotations

import re
from datetime import date
from typing import Any

IATA = re.compile(r"^[A-Z]{3}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_CABINS = {"economy", "premium_economy", "business", "first"}


def clean_query(raw: dict[str, Any]) -> dict[str, Any]:
    origin = str(raw.get("origin", "")).upper()
    destination = str(raw.get("destination", "")).upper()
    outbound = str(raw.get("outboundDate", ""))
    returning = str(raw.get("returnDate", ""))
    cabin = str(raw.get("cabin", "economy"))
    if not IATA.fullmatch(origin) or not IATA.fullmatch(destination):
        raise ValueError("origin and destination must be three-letter IATA codes")
    if not DATE.fullmatch(outbound) or not DATE.fullmatch(returning):
        raise ValueError("outboundDate and returnDate must use YYYY-MM-DD")
    if date.fromisoformat(returning) < date.fromisoformat(outbound):
        raise ValueError("returnDate cannot be before outboundDate")
    if cabin not in ALLOWED_CABINS:
        raise ValueError("unsupported cabin")
    return {
        **raw,
        "origin": origin,
        "destination": destination,
        "outboundDate": outbound,
        "returnDate": returning,
        "cabin": cabin,
        "adults": max(1, min(4, int(raw.get("adults") or 1))),
        "children": max(0, min(3, int(raw.get("children") or 0))),
        "checkedBags": max(0, min(2, int(raw.get("checkedBags") or 0))),
    }


def result(
    *, provider: str, airline: str = "", departure_time: str = "",
    arrival_time: str = "", duration_text: str = "", stops: int | None = None,
    price: float | int | None = None, source_url: str = "",
    price_scope: str = "unknown", tax_included: bool = False,
    passenger_count: int | None = None, checked_bags: int = 0,
    bookable: bool = False, airline_code: str = "",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "source": provider,
        "airline": airline,
        "airlineCode": airline_code,
        "departureTime": departure_time,
        "arrivalTime": arrival_time,
        "durationText": duration_text,
        "stops": stops,
        "price": price,
        "priceScope": price_scope,
        "taxIncluded": tax_included,
        "passengerCount": passenger_count,
        "checkedBags": checked_bags,
        "bookable": bookable,
        "sourceUrl": source_url,
    }


def challenge_page(text: str) -> bool:
    value = (text or "").lower()
    phrases = (
        "verify you are human", "captcha", "access denied", "unusual traffic",
        "請完成驗證", "驗證您是人類", "存取遭拒",
    )
    return any(phrase in value for phrase in phrases)
