"""Tests for the concurrent search layer and the listing backends.

No network: the sources are driven through a fake transport, which is what
lets the concurrency, retry, and rate-limiting behaviour be asserted at all.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from househunt.listings.models import House, Listing
from househunt.listings.search import (
    ConcurrentSearch,
    RateLimiter,
    SearchCriteria,
    dedupe,
)
from househunt.listings.sources import (
    NjmlsPortalSource,
    ResoWebApiSource,
    TermsNotAcknowledged,
    parse_njmls_html,
)


def make_listing(addr: str, zip_code: str = "07450", price: int = 600_000, **kw) -> Listing:
    return Listing(
        house=House(street_address=addr, city="Ridgewood", state="NJ", zip_code=zip_code,
                    beds=kw.pop("beds", 3)),
        list_price=price,
        **kw,
    )


class FakeSource:
    """Records call order and can be told to fail."""

    name = "fake"

    def __init__(self, fail_zips: set[str] | None = None, delay: float = 0.0):
        self.fail_zips = fail_zips or set()
        self.delay = delay
        self.calls: list[str] = []
        self.attempts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.max_concurrent = 0
        self._active = 0

    def search_zip(self, zip_code: str, criteria: SearchCriteria) -> list[Listing]:
        with self._lock:
            self.calls.append(zip_code)
            self.attempts[zip_code] = self.attempts.get(zip_code, 0) + 1
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if zip_code in self.fail_zips:
                raise ValueError(f"boom {zip_code}")
            return [make_listing(f"1 Main St {zip_code}", zip_code)]
        finally:
            with self._lock:
                self._active -= 1


class RateLimiterTest(unittest.TestCase):
    def test_limits_rate_across_threads(self):
        limiter = RateLimiter(requests_per_second=20)  # 50ms apart
        started = time.monotonic()
        threads = [threading.Thread(target=limiter.acquire) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Six acquisitions at 50ms spacing cannot finish in under ~250ms.
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_zero_rate_is_unlimited(self):
        limiter = RateLimiter(requests_per_second=0)
        started = time.monotonic()
        for _ in range(50):
            limiter.acquire()
        self.assertLess(time.monotonic() - started, 0.1)


class ConcurrentSearchTest(unittest.TestCase):
    def test_searches_every_zip(self):
        source = FakeSource()
        results = ConcurrentSearch(source, requests_per_second=0).run(
            ["07450", "07024", "07030"]
        )
        self.assertEqual(sorted(source.calls), ["07024", "07030", "07450"])
        self.assertEqual(len(results.listings), 3)

    def test_runs_concurrently(self):
        source = FakeSource(delay=0.05)
        ConcurrentSearch(source, max_workers=4, requests_per_second=0).run(
            [f"0700{i}" for i in range(4)]
        )
        self.assertGreater(source.max_concurrent, 1, "queries should overlap")

    def test_one_failing_zip_does_not_kill_the_run(self):
        source = FakeSource(fail_zips={"07024"})
        results = ConcurrentSearch(source, requests_per_second=0, max_retries=0).run(
            ["07450", "07024", "07030"]
        )
        self.assertEqual(len(results.listings), 2)
        self.assertEqual([r.zip_code for r in results.failures], ["07024"])
        self.assertIn("boom", results.failures[0].error)

    def test_retries_failures(self):
        source = FakeSource(fail_zips={"07024"})
        search = ConcurrentSearch(source, requests_per_second=0, max_retries=2)
        search._search_one("07024", SearchCriteria())
        self.assertEqual(source.attempts["07024"], 3, "initial try plus two retries")

    def test_commute_facts_attached_to_listings(self):
        source = FakeSource()
        results = ConcurrentSearch(source, requests_per_second=0).run(
            ["07450", "07024"], commute_by_zip={"07450": (45, 0), "07024": (35, 0)}
        )
        by_zip = {l.house.zip_code: l for l in results.listings}
        self.assertEqual(by_zip["07450"].commute_minutes, 45)
        self.assertEqual(by_zip["07024"].commute_minutes, 35)

    def test_sorted_by_commute_puts_shortest_first(self):
        source = FakeSource()
        results = ConcurrentSearch(source, requests_per_second=0).run(
            ["07450", "07024"], commute_by_zip={"07450": (45, 0), "07024": (35, 0)}
        )
        self.assertEqual(results.sorted_by_commute()[0].house.zip_code, "07024")

    def test_summary_reports_counts(self):
        source = FakeSource(fail_zips={"07024"})
        results = ConcurrentSearch(source, requests_per_second=0, max_retries=0).run(
            ["07450", "07024"]
        )
        self.assertIn("1 listings", results.summary())
        self.assertIn("1 failed", results.summary())


class DedupeTest(unittest.TestCase):
    def test_same_address_collapses(self):
        a = make_listing("1 Main St")
        b = make_listing("1 MAIN ST")  # case difference only
        self.assertEqual(len(dedupe([a, b])), 1)

    def test_keeps_better_commute(self):
        a = make_listing("1 Main St")
        a.commute_minutes = 60
        b = make_listing("1 Main St")
        b.commute_minutes = 30
        self.assertEqual(dedupe([a, b])[0].commute_minutes, 30)

    def test_distinct_addresses_survive(self):
        self.assertEqual(len(dedupe([make_listing("1 Main St"), make_listing("2 Main St")])), 2)


class ResoSourceTest(unittest.TestCase):
    def source(self) -> ResoWebApiSource:
        return ResoWebApiSource("https://api.example.com/reso/odata", "token123")

    def test_filter_includes_every_criterion(self):
        f = self.source().build_filter(
            "07450", SearchCriteria(min_price=500_000, max_price=850_000, min_beds=2)
        )
        self.assertIn("PostalCode eq '07450'", f)
        self.assertIn("ListPrice ge 500000", f)
        self.assertIn("ListPrice le 850000", f)
        self.assertIn("BedroomsTotal ge 2", f)
        self.assertIn("StandardStatus eq 'Active'", f)

    def test_filter_escapes_quotes(self):
        f = self.source().build_filter("07450", SearchCriteria(statuses=("O'Brien",)))
        self.assertIn("'O''Brien'", f)

    def test_maps_reso_fields_onto_the_model(self):
        listing = self.source().to_listing(
            {
                "UnparsedAddress": "12 Oak St",
                "City": "Ridgewood",
                "StateOrProvince": "NJ",
                "PostalCode": "07450",
                "BedroomsTotal": 3,
                "BathroomsTotalInteger": 2,
                "LivingArea": 1800,
                "ListPrice": 725000,
                "DaysOnMarket": 11,
                "StandardStatus": "Active",
                "ListingId": "24001234",
                "Latitude": 40.9793,
                "Longitude": -74.1163,
            }
        )
        self.assertEqual(listing.house.street_address, "12 Oak St")
        self.assertEqual(listing.house.beds, 3)
        self.assertEqual(listing.list_price, 725000)
        self.assertEqual(listing.mls_id, "24001234")
        self.assertEqual(listing.house.latitude, 40.9793)
        self.assertEqual(listing.source, "reso")


class NjmlsSourceTest(unittest.TestCase):
    def test_refuses_to_run_without_terms_acknowledgement(self):
        with self.assertRaises(TermsNotAcknowledged):
            NjmlsPortalSource()

    def test_builds_search_params(self):
        source = NjmlsPortalSource(acknowledge_terms=True, respect_robots=False)
        params = source.build_params("07450", SearchCriteria(min_price=500_000, min_beds=2))
        self.assertEqual(params["zipcode"], "07450")
        self.assertEqual(params["pricemin"], "500000")
        self.assertEqual(params["beds"], "2")

    def test_parses_jsonld_listings(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type":"ItemList","itemListElement":[
          {"@type":"SingleFamilyResidence","url":"https://x/1",
           "address":{"streetAddress":"12 Oak St","addressLocality":"Ridgewood",
                      "addressRegion":"NJ","postalCode":"07450"},
           "numberOfBedrooms":3,"numberOfBathroomsTotal":2,
           "offers":{"price":725000}}]}
        </script></head><body></body></html>
        """
        rows = parse_njmls_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "12 Oak St")
        self.assertEqual(rows[0]["price"], 725000)
        self.assertEqual(rows[0]["zip"], "07450")

    def test_parser_tolerates_junk(self):
        self.assertEqual(parse_njmls_html("<html>no json here</html>"), [])
        self.assertEqual(
            parse_njmls_html('<script type="application/ld+json">{broken</script>'), []
        )

    def test_custom_parser_is_used(self):
        called = []

        def fake_parser(html: str):
            called.append(html)
            return []

        source = NjmlsPortalSource(
            acknowledge_terms=True, respect_robots=False, parse_html=fake_parser
        )
        self.assertIs(source._parse_html, fake_parser)


if __name__ == "__main__":
    unittest.main()
