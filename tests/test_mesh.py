import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from xiaoao_mesh.core import challenge_page, clean_query
from xiaoao_mesh.planner import build_scan_matrix, date_pairs, query_key, scan_page
from xiaoao_mesh.providers.google_playwright import google_flights_url, parse_result_label
from xiaoao_mesh.quality import classify_price_opportunity, comparable_family_price, deduplicate_offers
from xiaoao_mesh.server import configured_provider_names
import xiaoao_mesh.server as server_module
from xiaoao_mesh.snapshots import SnapshotStore


QUERY = {
    "origin": "hkg", "destination": "KUL",
    "outboundDate": "2026-12-19", "returnDate": "2026-12-25",
    "cabin": "economy", "adults": 2, "children": 1, "checkedBags": 1,
}

PLAN = {
    "holidayStart": "2026-12-19", "holidayEnd": "2026-12-27",
    "minDays": 4, "maxDays": 8,
    "origins": ["HKG", "MFM", "CAN", "SZX"],
    "destinations": ["ICN", "KIX"], "cabins": ["economy", "business"],
    "adults": 2, "children": 1, "checkedBags": 0,
}


class MeshTests(unittest.TestCase):
    def test_query_normalization(self):
        cleaned = clean_query(QUERY)
        self.assertEqual(cleaned["origin"], "HKG")
        self.assertEqual(cleaned["adults"], 2)

    def test_challenge_detection(self):
        self.assertTrue(challenge_page("Please verify you are human"))
        self.assertFalse(challenge_page("HK$ 7,518 return flight"))

    def test_provider_allowlist_and_no_blind_ota_regex_defaults(self):
        previous = os.environ.get("FLIGHT_MESH_PROVIDERS")
        try:
            os.environ["FLIGHT_MESH_PROVIDERS"] = "google-playwright,trip,fast-flights,google-playwright"
            self.assertEqual(configured_provider_names(), ["google-playwright", "fast-flights"])
        finally:
            if previous is None:
                os.environ.pop("FLIGHT_MESH_PROVIDERS", None)
            else:
                os.environ["FLIGHT_MESH_PROVIDERS"] = previous

    def test_holiday_window_has_all_fifteen_date_pairs(self):
        pairs = date_pairs("2026-12-19", "2026-12-27", 4, 8)
        self.assertEqual(len(pairs), 15)
        self.assertEqual(len({(item.outbound, item.returning) for item in pairs}), 15)
        self.assertTrue(all("2026-12-19" <= item.outbound < item.returning <= "2026-12-27" for item in pairs))

    def test_matrix_counts_dates_routes_and_cabins_honestly(self):
        matrix = build_scan_matrix(PLAN)
        self.assertEqual(len(matrix), 15 * 4 * 2 * 2)
        first_sweep = matrix[:4 * 2 * 2]
        self.assertEqual({item["destination"] for item in first_sweep}, {"ICN", "KIX"})
        self.assertEqual(len({query_key(item) for item in matrix}), len(matrix))

    def test_scan_page_skips_completed_and_prioritizes_shortlist(self):
        matrix = build_scan_matrix(PLAN)
        completed = [query_key(matrix[0])]
        priority = [query_key(matrix[20])]
        page = scan_page(PLAN, limit=3, completed_keys=completed, priority_keys=priority)
        self.assertEqual(query_key(page["queries"][0]), priority[0])
        self.assertNotIn(completed[0], {query_key(item) for item in page["queries"]})
        self.assertEqual(page["matrixTotal"], len(matrix))
        self.assertEqual(page["datePairs"], 15)

    def test_google_url_carries_family_and_exact_itinerary(self):
        url = google_flights_url(clean_query(QUERY))
        self.assertIn("/travel/flights/search?tfs=", url)
        self.assertIn("curr=HKD", url)
        self.assertIn("bags=1", url)

    def test_google_accessibility_label_is_structured(self):
        parsed = parse_result_label(
            "來回總價 7,649 港幣。搭乘長榮航空的直達航班。晚上8:10 至 晚上9:35。"
        )
        self.assertEqual(parsed["price"], 7649)
        self.assertEqual(parsed["airline"], "長榮航空")
        self.assertEqual(parsed["stops"], 0)

    def test_only_confirmed_family_tax_price_is_comparable(self):
        base = {"price": 7000, "passengerCount": 3, "priceScope": "family", "taxIncluded": True}
        self.assertEqual(comparable_family_price(base, 3), 7000)
        self.assertIsNone(comparable_family_price({**base, "priceScope": "unknown"}, 3))
        self.assertIsNone(comparable_family_price({**base, "passengerCount": 1}, 3))
        self.assertIsNone(comparable_family_price({**base, "taxIncluded": False}, 3))

    def test_consensus_detects_cross_source_conflict(self):
        query = clean_query(QUERY)
        common = {
            "airline": "長榮航空", "departureTime": "20:10", "priceScope": "family",
            "passengerCount": 3, "taxIncluded": True,
        }
        offers = deduplicate_offers(query, [
            {**common, "provider": "google-playwright", "price": 7000},
            {**common, "provider": "serpapi-google-flights", "price": 7700},
        ])
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0]["sourceConflict"])
        self.assertEqual(offers[0]["verificationState"], "conflict")

    def test_god_price_requires_history_freshness_and_two_sources(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        offer = {
            "provider": "google-playwright", "price": 6000, "priceScope": "family",
            "passengerCount": 3, "taxIncluded": True, "independentSourceCount": 2,
            "fetchedAt": "2026-08-31T11:55:00Z", "sourceConflict": False,
        }
        result = classify_price_opportunity(offer, [9000, 8800, 9200, 8700, 9100], passenger_count=3, now=now)
        self.assertTrue(result["godPrice"])
        self.assertFalse(classify_price_opportunity({**offer, "independentSourceCount": 1},
            [9000, 8800, 9200, 8700, 9100], passenger_count=3, now=now)["godPrice"])

    def test_snapshot_is_rejected_when_timestamp_is_from_future(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(os.path.join(directory, "mesh.sqlite3"))
            query = clean_query(QUERY)
            store.put(query, [{"price": 7000}], "2099-08-31T11:00:00Z")
            self.assertIsNone(store.get(query, max_age_hours=72))


class _FakeProvider:
    def __init__(self, name, price=None, fail=False):
        self.name, self.price, self.fail = name, price, fail

    async def search(self, query):
        if self.fail:
            raise RuntimeError("temporary source failure")
        return [{
            "provider": self.name, "source": self.name, "airline": "長榮航空",
            "departureTime": "20:10", "price": self.price,
            "priceScope": "family", "passengerCount": 3, "taxIncluded": True,
            "bookable": False,
        }]


class ServerBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server_module.PROVIDER_HEALTH.clear()
        self.directory = tempfile.TemporaryDirectory()
        server_module.SNAPSHOTS = SnapshotStore(os.path.join(self.directory.name, "mesh.sqlite3"))

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_batch_combines_independent_sources(self):
        providers = {
            "google-playwright": _FakeProvider("google-playwright", 7000),
            "fast-flights": _FakeProvider("fast-flights", 7050),
        }
        with patch.dict(os.environ, {"FLIGHT_MESH_PROVIDERS": "google-playwright,fast-flights"}), \
                patch.object(server_module, "make_provider", side_effect=lambda name: providers[name]):
            response = await server_module.search_batch([QUERY])
        result = response["searches"][0]["results"][0]
        self.assertEqual(result["independentSourceCount"], 2)
        self.assertFalse(result["sourceConflict"])
        self.assertEqual(response["coverage"]["completed"], 1)

    async def test_failed_live_sources_return_snapshot_without_fresh_verification(self):
        query = clean_query(QUERY)
        server_module.SNAPSHOTS.put(query, [{
            "provider": "google-playwright", "price": 7000, "priceScope": "family",
            "passengerCount": 3, "taxIncluded": True,
        }], datetime.now(timezone.utc).isoformat())
        providers = {
            "google-playwright": _FakeProvider("google-playwright", fail=True),
            "fast-flights": _FakeProvider("fast-flights", fail=True),
        }
        with patch.dict(os.environ, {"FLIGHT_MESH_PROVIDERS": "google-playwright,fast-flights"}), \
                patch.object(server_module, "make_provider", side_effect=lambda name: providers[name]):
            response = await server_module.search_batch([QUERY])
        result = response["searches"][0]["results"][0]
        self.assertEqual(result["priceFreshness"], "snapshot")
        self.assertEqual(result["verifiedAt"], "")
        self.assertEqual(response["snapshotsUsed"], 1)


if __name__ == "__main__":
    unittest.main()
