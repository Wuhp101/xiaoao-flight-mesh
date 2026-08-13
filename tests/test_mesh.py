import os
import unittest

from xiaoao_mesh.core import challenge_page, clean_query
from xiaoao_mesh.providers.metasearch import source_url
from xiaoao_mesh.server import configured_provider_names


QUERY = {
    "origin": "hkg", "destination": "KUL",
    "outboundDate": "2026-12-16", "returnDate": "2026-12-27",
    "cabin": "economy", "adults": 2, "children": 1, "checkedBags": 1,
}


class MeshTests(unittest.TestCase):
    def test_query_normalization(self):
        cleaned = clean_query(QUERY)
        self.assertEqual(cleaned["origin"], "HKG")
        self.assertEqual(cleaned["adults"], 2)

    def test_source_urls_are_non_google_and_route_specific(self):
        query = clean_query(QUERY)
        for provider in ("skyscanner", "trip", "kayak", "expedia"):
            url = source_url(provider, query)
            self.assertNotIn("google", url)
            self.assertIn("hkg", url.lower())
            self.assertIn("kul", url.lower())

    def test_challenge_detection(self):
        self.assertTrue(challenge_page("Please verify you are human"))
        self.assertFalse(challenge_page("HK$ 7,518 return flight"))

    def test_provider_allowlist(self):
        previous = os.environ.get("FLIGHT_MESH_PROVIDERS")
        try:
            os.environ["FLIGHT_MESH_PROVIDERS"] = "trip,unknown,kayak,trip"
            self.assertEqual(configured_provider_names(), ["trip", "kayak"])
        finally:
            if previous is None:
                os.environ.pop("FLIGHT_MESH_PROVIDERS", None)
            else:
                os.environ["FLIGHT_MESH_PROVIDERS"] = previous


if __name__ == "__main__":
    unittest.main()
