"""Concurrent multi-ZIP listing search.

The commute pipeline produces a ranked ZIP list; this runs a search across all
of those ZIPs at once against whichever :class:`ListingSource` is configured,
and folds the results back into :class:`~househunt.listings.models.Listing`
objects with their commute facts attached.

Concurrency is bounded on two axes deliberately: a worker pool caps how many
ZIP queries are in flight, and a shared token bucket caps the request rate
across *all* workers. Raising the pool size without the rate limit is how you
get blocked by a listing provider.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, Sequence

from .models import Listing


@dataclass
class SearchCriteria:
    """What counts as a house worth seeing."""

    min_price: int | None = 500_000
    max_price: int | None = 850_000
    min_beds: int | None = 2
    min_baths: float | None = None
    min_sq_ft: int | None = None
    property_types: tuple[str, ...] = ("Residential",)
    statuses: tuple[str, ...] = ("Active",)
    #: Hard cap per ZIP, so one dense ZIP cannot dominate a run.
    limit_per_zip: int = 250

    def describe(self) -> str:
        lo = f"${self.min_price:,}" if self.min_price else "any"
        hi = f"${self.max_price:,}" if self.max_price else "any"
        return f"{lo}-{hi}, {self.min_beds or 0}+ beds, {'/'.join(self.statuses)}"


class ListingSource(Protocol):
    """A backend that can return listings for one ZIP code."""

    name: str

    def search_zip(self, zip_code: str, criteria: SearchCriteria) -> list[Listing]:
        ...


class RateLimiter:
    """Thread-safe token bucket, shared by every worker."""

    def __init__(self, requests_per_second: float, burst: int = 1):
        self.interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.burst = max(burst, 1)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            # Allow a small burst by letting the schedule sit slightly behind.
            earliest = max(now - self.interval * (self.burst - 1), self._next_at)
            wait = max(0.0, earliest - now)
            self._next_at = earliest + self.interval
        if wait:
            time.sleep(wait)


@dataclass
class ZipResult:
    zip_code: str
    listings: list[Listing] = field(default_factory=list)
    error: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class SearchResults:
    results: list[ZipResult] = field(default_factory=list)

    @property
    def listings(self) -> list[Listing]:
        return [l for r in self.results for l in r.listings]

    @property
    def failures(self) -> list[ZipResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        ok = [r for r in self.results if r.ok]
        return (
            f"{len(self.listings)} listings across {len(ok)}/{len(self.results)} ZIPs"
            + (f"; {len(self.failures)} failed" if self.failures else "")
        )

    def sorted_by_commute(self) -> list[Listing]:
        """Cheapest commute first, then cheapest house."""
        return sorted(
            self.listings,
            key=lambda l: (
                l.commute_minutes if l.commute_minutes is not None else 10**6,
                l.list_price if l.list_price is not None else 10**9,
            ),
        )


class ConcurrentSearch:
    """Runs a :class:`ListingSource` across many ZIPs in parallel."""

    def __init__(
        self,
        source: ListingSource,
        max_workers: int = 6,
        requests_per_second: float = 1.0,
        cache: Any = None,
        on_progress: Callable[[ZipResult], None] | None = None,
        max_retries: int = 2,
    ):
        self.source = source
        self.max_workers = max(1, max_workers)
        self.limiter = RateLimiter(requests_per_second)
        self.cache = cache
        self.on_progress = on_progress
        self.max_retries = max_retries

    def _search_one(self, zip_code: str, criteria: SearchCriteria) -> ZipResult:
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                listings = self.source.search_zip(zip_code, criteria)
                return ZipResult(
                    zip_code=zip_code,
                    listings=listings,
                    elapsed_seconds=time.monotonic() - started,
                )
            except Exception as exc:  # noqa: BLE001 - one bad ZIP must not kill the run
                last_error = exc
                if attempt < self.max_retries:
                    # Back off before retrying; the usual cause is throttling.
                    time.sleep(2.0 * (attempt + 1))
        return ZipResult(
            zip_code=zip_code,
            error=f"{type(last_error).__name__}: {last_error}",
            elapsed_seconds=time.monotonic() - started,
        )

    def run(
        self,
        zip_codes: Sequence[str],
        criteria: SearchCriteria | None = None,
        commute_by_zip: dict[str, tuple[int, int]] | None = None,
    ) -> SearchResults:
        """Search every ZIP concurrently.

        ``commute_by_zip`` maps ZIP -> ``(minutes, transfers)`` from the
        commute pipeline; when supplied, every returned listing carries its
        commute so the final list can be sorted by it.
        """
        criteria = criteria or SearchCriteria()
        out = SearchResults()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._search_one, z, criteria): z for z in zip_codes
            }
            for future in as_completed(futures):
                result = future.result()
                if commute_by_zip:
                    facts = commute_by_zip.get(result.zip_code)
                    if facts:
                        minutes, transfers = facts
                        for listing in result.listings:
                            listing.commute_minutes = minutes
                            listing.commute_transfers = transfers
                if self.cache is not None:
                    for listing in result.listings:
                        self.cache.insert_listing(listing)
                out.results.append(result)
                if self.on_progress:
                    self.on_progress(result)

        out.results.sort(key=lambda r: r.zip_code)
        return out


def dedupe(listings: Iterable[Listing]) -> list[Listing]:
    """Collapse listings that describe the same address.

    The same house shows up twice when ZIP boundaries and MLS records
    disagree, or when a listing is re-entered with a new MLS ID.
    """
    seen: dict[str, Listing] = {}
    for listing in listings:
        key = listing.hsh
        current = seen.get(key)
        if current is None:
            seen[key] = listing
            continue
        # Keep the one with the better commute, then the newer listing.
        def rank(l: Listing) -> tuple[int, int]:
            return (
                l.commute_minutes if l.commute_minutes is not None else 10**6,
                l.days_on_market if l.days_on_market is not None else 10**6,
            )

        if rank(listing) < rank(current):
            seen[key] = listing
    return list(seen.values())
