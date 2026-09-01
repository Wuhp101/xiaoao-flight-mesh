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
    """Browserless Google Flights candidate adapter.

    The first pass uses faster-flights' light HTML path. A separate shopping
    RPC method is available for background recovery of routes that the first
    pass cannot see. Both outputs are unverified hints only.
    """

    name = "fast-flights"

    @staticmethod
    def _imports():
        try:
            from fast_flights import FlightQuery, Passengers, ShoppingOptions, create_query, get_flights
        except ImportError as error:
            raise RuntimeError("faster-flights dependency is not installed") from error
        return FlightQuery, Passengers, ShoppingOptions, create_query, get_flights

    def _request(self, query: dict[str, Any]):
        FlightQuery, Passengers, _, create_query, _ = self._imports()
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
        return create_query(
            flights=flights,
            trip="round-trip",
            seat=query["cabin"].replace("_", "-"),
            passengers=passengers,
            currency="HKD",
            language="zh-TW",
        )

    def _results(self, query: dict[str, Any], offers: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for offer in list(offers or [])[:8]:
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

            def format_time(value: Any) -> str:
                if isinstance(value, (list, tuple)):
                    return ":".join(f"{int(part):02d}" for part in value)
                return str(value or "")

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
                passenger_count=query["adults"] + query["children"],
                checked_bags=0,
            ))
        return output

    async def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        # faster-flights 3.8 does not expose the old checked_bags query knob.
        # Never pretend a browserless hint satisfies a requested baggage rule.
        if int(query.get("checkedBags") or 0) > 0:
            return []
        _, _, _, _, get_flights = self._imports()
        request = self._request(query)
        offers = await asyncio.to_thread(get_flights, request)
        return self._results(query, offers)

    async def search_shopping(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """Second-wave recovery using Google's shopping RPC.

        This is intentionally separate from ``search`` because some routes can
        take much longer. The workflow ingests first-pass candidates before
        calling this method, so slow recovery never blocks the first screen.
        """
        if int(query.get("checkedBags") or 0) > 0:
            return []
        _, _, ShoppingOptions, _, get_flights = self._imports()
        request = self._request(query)
        shopping = ShoppingOptions(ranking_mode="cheapest", result_sort="price")
        offers = await asyncio.to_thread(get_flights, request, shopping=shopping)
        return self._results(query, offers)
