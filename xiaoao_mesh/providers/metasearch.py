from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ..core import challenge_page, result

PRICE = re.compile(r"(?:HK\$|HKD)\s*([\d,]{3,})", re.I)


def source_url(name: str, query: dict[str, Any]) -> str:
    origin = query["origin"].lower()
    destination = query["destination"].lower()
    outbound = query["outboundDate"]
    returning = query["returnDate"]
    adults = query["adults"]
    children = query["children"]
    if name == "skyscanner":
        params = urlencode({
            "adultsv2": adults,
            "childrenv2": ",".join(["8"] * children),
            "cabinclass": query["cabin"].replace("_", ""),
            "currency": "HKD", "locale": "zh-TW", "market": "HK",
        })
        return f"https://www.skyscanner.com/transport/flights/{origin}/{destination}/{outbound.replace('-', '')}/{returning.replace('-', '')}/?{params}"
    if name == "trip":
        params = urlencode({
            "dcity": origin, "acity": destination, "ddate": outbound,
            "rdate": returning, "triptype": "rt", "class": query["cabin"],
            "quantity": adults, "childqty": children,
        })
        return f"https://www.trip.com/flights/showfarefirst?{params}"
    if name == "kayak":
        travellers = f"{adults}adults" + (f"/{children}children" if children else "")
        return f"https://www.kayak.com/flights/{query['origin']}-{query['destination']}/{outbound}/{returning}/{travellers}?sort=bestflight_a"
    if name == "expedia":
        outbound_us = f"{outbound[5:7]}/{outbound[8:10]}/{outbound[:4]}"
        returning_us = f"{returning[5:7]}/{returning[8:10]}/{returning[:4]}"
        params = urlencode({
            "leg1": f"from:{query['origin']},to:{query['destination']},departure:{outbound_us}TANYT",
            "leg2": f"from:{query['destination']},to:{query['origin']},departure:{returning_us}TANYT",
            "passengers": f"adults:{adults},children:{children}",
            "mode": "search",
        })
        return f"https://www.expedia.com.hk/Flights-Search?{params}"
    raise ValueError(f"unknown metasearch source: {name}")


class MetaSearchProvider:
    """Public-page adapter. It stops on challenges and never bypasses anti-bot controls."""

    def __init__(self, name: str, timeout_ms: int = 45_000):
        if name not in {"skyscanner", "trip", "kayak", "expedia"}:
            raise ValueError("unsupported metasearch provider")
        self.name = name
        self.timeout_ms = timeout_ms

    async def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise RuntimeError("playwright dependency is not installed") from error
        url = source_url(self.name, query)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(locale="zh-TW")
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(4_000)
                body = await page.locator("body").inner_text(timeout=self.timeout_ms)
                if challenge_page(body):
                    raise RuntimeError(f"{self.name} requested human verification")
                prices = []
                for match in PRICE.finditer(body):
                    value = int(match.group(1).replace(",", ""))
                    if 200 <= value <= 500_000 and value not in prices:
                        prices.append(value)
                # Public result pages change frequently. Only emit an explicitly labelled
                # reference price; the app will require a manual official-channel check.
                return [result(
                    provider=self.name,
                    price=value,
                    source_url=page.url,
                    price_scope="unknown",
                    tax_included=False,
                ) for value in prices[:5]]
            finally:
                await browser.close()
