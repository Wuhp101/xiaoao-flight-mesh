from __future__ import annotations

import asyncio
import base64
import re
from typing import Any
from urllib.parse import quote

from ..core import challenge_page, result


CABIN_CODES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
RESULT_SELECTOR = '[role="link"][aria-label*="來回總價"], [role="link"][aria-label*="round trip total price" i]'
TIME = re.compile(r"(?:凌晨|清晨|上午|中午|下午|傍晚|晚上)?\d{1,2}:\d{2}")


def _varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(data)) + data


def _location(iata: str) -> bytes:
    return _field_varint(1, 1) + _field_bytes(2, iata)


def _leg(day: str, origin: str, destination: str) -> bytes:
    return _field_bytes(2, day) + _field_bytes(13, _location(origin)) + _field_bytes(14, _location(destination))


def google_flights_url(query: dict[str, Any]) -> str:
    passengers = b"".join(_field_varint(8, 1) for _ in range(query["adults"]))
    passengers += b"".join(_field_varint(8, 2) for _ in range(query["children"]))
    unrestricted_stops = bytes([0x08, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01])
    message = b"".join((
        _field_varint(1, 28), _field_varint(2, 1),
        _field_bytes(3, _leg(query["outboundDate"], query["origin"], query["destination"])),
        _field_bytes(3, _leg(query["returnDate"], query["destination"], query["origin"])),
        passengers, _field_varint(9, CABIN_CODES[query["cabin"]]), _field_varint(14, 1),
        _field_bytes(16, unrestricted_stops), _field_varint(19, 1),
    ))
    tfs = base64.urlsafe_b64encode(message).decode().rstrip("=")
    bags = f"&bags={query['checkedBags']}" if query.get("checkedBags") else ""
    return f"https://www.google.com/travel/flights/search?tfs={quote(tfs)}&hl=zh-TW&gl=HK&curr=HKD{bags}"


def parse_result_label(label: str, href: str = "") -> dict[str, Any] | None:
    text = " ".join(str(label or "").split())
    price_match = re.search(r"來回總價\s*([\d,]+)\s*港幣", text)
    if not price_match:
        price_match = re.search(r"(?:round trip total price|total)\s*(?:HK\$|HKD)?\s*([\d,]+)", text, re.I)
    if not price_match:
        return None
    airline_match = re.search(r"搭乘(.+?)的(?:直達航班|航班)", text)
    if not airline_match:
        airline_match = re.search(r"(?:with|on)\s+(.+?)(?:\.|,|nonstop|flight)", text, re.I)
    times = TIME.findall(text)
    stop_match = re.search(r"(?:中途停留|需轉機)\s*(\d+)\s*次|(\d+)\s*個(?:停靠站|轉機點)", text)
    stops = 0 if "直達航班" in text or re.search(r"\bnonstop\b", text, re.I) else None
    if stops is None and stop_match:
        stops = int(stop_match.group(1) or stop_match.group(2))
    duration = re.search(r"總交通時間：(.+?)\s*選擇航班", text)
    return {
        "airline": airline_match.group(1).strip() if airline_match else "",
        "departure_time": times[0] if times else "",
        "arrival_time": times[1] if len(times) > 1 else "",
        "duration_text": duration.group(1).strip() if duration else "",
        "stops": stops,
        "price": int(price_match.group(1).replace(",", "")),
        "source_url": href,
    }


class GooglePlaywrightProvider:
    name = "google-playwright"

    def __init__(self, timeout_ms: int = 45_000, pages: int = 2):
        self.timeout_ms = timeout_ms
        self._semaphore = asyncio.Semaphore(max(1, min(3, pages)))
        self._playwright = None
        self._browser = None

    async def start(self) -> None:
        if self._browser:
            return
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _submit_if_needed(self, page: Any) -> None:
        exact = page.get_by_role("button", name=re.compile(r"^(搜尋航班|Search flights)$", re.I))
        if await exact.count() == 1:
            await exact.click()

    async def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        await self.start()
        async with self._semaphore:
            page = await self._browser.new_page(locale="zh-TW", viewport={"width": 1280, "height": 900})
            url = google_flights_url(query)
            try:
                labels: list[dict[str, str]] = []
                body = ""
                for attempt in range(2):
                    await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    locator = page.locator(RESULT_SELECTOR)
                    try:
                        await locator.first.wait_for(timeout=8_000 if attempt == 0 else 18_000)
                    except Exception:
                        await self._submit_if_needed(page)
                        try:
                            await locator.first.wait_for(timeout=18_000)
                        except Exception:
                            pass
                    body = await page.locator("body").inner_text(timeout=self.timeout_ms)
                    if challenge_page(body):
                        raise RuntimeError("google requested human verification")
                    labels = await locator.evaluate_all("""els => els.slice(0, 20).map(el => ({
                        label: el.getAttribute('aria-label') || '',
                        href: el.href || (el.closest('a') && el.closest('a').href) || location.href
                    }))""")
                    if labels:
                        break
                    await page.wait_for_timeout(750)
                passenger_count = query["adults"] + query["children"]
                family = bool(re.search(
                    rf"{passenger_count}\s*位乘客的價格\s*\(含稅及其他費用\)|Prices?\s+(?:shown\s+)?(?:is|are)\s+for\s+{passenger_count}\s+passengers?",
                    body, re.I,
                ))
                output: list[dict[str, Any]] = []
                seen: set[tuple[Any, ...]] = set()
                for item in labels:
                    parsed = parse_result_label(item.get("label", ""), item.get("href", "") or page.url)
                    if not parsed:
                        continue
                    key = (parsed["price"], parsed["airline"], parsed["departure_time"], parsed["arrival_time"])
                    if key in seen:
                        continue
                    seen.add(key)
                    output.append(result(
                        provider=self.name, airline=parsed["airline"], departure_time=parsed["departure_time"],
                        arrival_time=parsed["arrival_time"], duration_text=parsed["duration_text"],
                        stops=parsed["stops"], price=parsed["price"], source_url=parsed["source_url"],
                        price_scope="family" if family else "unknown", tax_included=family,
                        passenger_count=passenger_count, checked_bags=query["checkedBags"], bookable=False,
                    ))
                if not output:
                    raise RuntimeError("google returned no readable flight results")
                return output[:10]
            finally:
                await page.close()
