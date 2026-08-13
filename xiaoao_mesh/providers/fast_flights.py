from __future__ import annotations

import asyncio
from typing import Any

from ..core import result


def _field(value: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _price(value: Any) -> float | None:
    raw = _field(value, "price", "total_price", default=None)
    if isinstance(raw, (int, float)):
        return float(raw)
    digits = "".join(character for character in str(raw or "") if character.isdigit() or character == ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


class FastFlightsProvider:
    """Google Flights protobuf adapter powered by the MIT fast-flights project."""

    name = "fast-flights"

    async def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            from fast_flights import FlightQuery, Passengers, create_query, get_flights
        except ImportError as error:
            raise RuntimeError("fast-flights dependency is not installed") from error

        passengers = Passengers(
            adults=query["adults"],
            children=query["children"],
            infants_in_seat=0,
            infants_on_lap=0,
        )
        flights = [
            FlightQuery(date=query["outboundDate"], from_airport=query["origin"], to_airport=query["destination"]),
            FlightQuery(date=query["returnDate"], from_airport=query["destination"], to_airport=query["origin"]),
        ]
        request = create_query(
            flights=flights,
            trip="round-trip",
            seat=query["cabin"].replace("_", "-"),
            passengers=passengers,
            currency="HKD",
            checked_bags=query["checkedBags"],
            language="zh-TW",
        )
        offers = await asyncio.to_thread(get_flights, request)
        output: list[dict[str, Any]] = []
        for offer in list(offers)[:8]:
            price = _price(offer)
            if price is None:
                continue
            legs = list(_field(offer, "flights", default=[]) or [])
            first_leg = legs[0] if legs else None
            last_leg = legs[-1] if legs else None
            departure = _field(first_leg, "departure", default=None)
            arrival = _field(last_leg, "arrival", default=None)
            departure_time = _field(departure, "time", default="")
            arrival_time = _field(arrival, "time", default="")
            format_time = lambda value: ":".join(f"{int(part):02d}" for part in value) if isinstance(value, (list, tuple)) else str(value or "")
            output.append(result(
                provider=self.name,
                airline="、".join(str(value) for value in (_field(offer, "airlines", default=[]) or [])),
                departure_time=format_time(departure_time),
                arrival_time=format_time(arrival_time),
                duration_text=f"{sum(int(_field(leg, 'duration', default=0) or 0) for leg in legs)} 分鐘" if legs else "",
                stops=max(0, len(legs) - 1),
                price=price,
                source_url="https://www.google.com/travel/flights",
                price_scope="unknown",
                tax_included=False,
            ))
        return output
