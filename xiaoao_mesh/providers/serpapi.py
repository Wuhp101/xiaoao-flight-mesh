from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from typing import Any

from ..core import result


class SerpApiGoogleFlightsProvider:
    name = "serpapi-google-flights"

    def __init__(self, token: str | None = None, timeout: int = 45):
        self.token = token or os.getenv("SERPAPI_KEY", "")
        self.timeout = timeout
        if not self.token:
            raise RuntimeError("SERPAPI_KEY is not configured")

    def _request(self, query: dict[str, Any]) -> dict[str, Any]:
        params = urllib.parse.urlencode({
            "engine": "google_flights", "api_key": self.token,
            "departure_id": query["origin"], "arrival_id": query["destination"],
            "outbound_date": query["outboundDate"], "return_date": query["returnDate"],
            "currency": "HKD", "hl": "zh-tw", "gl": "hk", "type": 1,
            "travel_class": {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}[query["cabin"]],
            "adults": query["adults"], "children": query["children"],
            "stops": query.get("maxStops", 2), "bags": query.get("checkedBags", 0),
        })
        request = urllib.request.Request(f"https://serpapi.com/search.json?{params}", headers={"User-Agent": "xiaoao-flight-mesh/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    async def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(self._request, query)
        if payload.get("error"):
            raise RuntimeError(str(payload["error"])[:240])
        passenger_count = query["adults"] + query["children"]
        insights = payload.get("price_insights") or {}
        typical_range = insights.get("typical_price_range") or []
        market_low = typical_range[0] if len(typical_range) >= 2 and isinstance(typical_range[0], (int, float)) else None
        market_high = typical_range[1] if len(typical_range) >= 2 and isinstance(typical_range[1], (int, float)) else None
        market_level = str(insights.get("price_level") or "").lower()
        if market_level not in {"low", "typical", "high"}:
            market_level = ""
        market_source = "google-price-insights" if market_level or market_low is not None else ""
        output: list[dict[str, Any]] = []
        for offer in list(payload.get("best_flights") or []) + list(payload.get("other_flights") or []):
            flights = offer.get("flights") or []
            first, last = (flights[0] if flights else {}), (flights[-1] if flights else {})
            price = offer.get("price")
            if not isinstance(price, (int, float)):
                continue
            offer_market_level = market_level
            if market_low is not None and market_high is not None:
                offer_market_level = "low" if price < market_low else "high" if price > market_high else "typical"
            elif output:
                offer_market_level = ""
            airline_names = list(dict.fromkeys(str(flight.get("airline") or "") for flight in flights if flight.get("airline")))
            output.append(result(
                provider=self.name, airline="、".join(airline_names),
                departure_time=str(first.get("departure_airport", {}).get("time") or ""),
                arrival_time=str(last.get("arrival_airport", {}).get("time") or ""),
                duration_text=f"{offer.get('total_duration')} 分鐘" if offer.get("total_duration") else "",
                stops=max(0, len(flights) - 1), price=price,
                source_url=str(payload.get("search_metadata", {}).get("google_flights_url") or "https://www.google.com/travel/flights"),
                price_scope="family", tax_included=True, passenger_count=passenger_count,
                checked_bags=query["checkedBags"], bookable=bool(offer.get("booking_token")),
                market_price_low=market_low, market_price_high=market_high,
                market_price_level=offer_market_level, market_price_source=market_source,
            ))
        return output[:20]
