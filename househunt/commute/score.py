"""Turn per-stop routing results into a ranked list of ZIP codes.

The output answers the question the house search actually needs: *which ZIP
codes should I be searching in?* A raw travel time is not enough for that. Two
towns can both be "45 minutes to Midtown" and be completely different places to
live:

* one has a train every 8 minutes, the other has four buses a morning;
* one is a one-seat ride, the other needs a transfer at Newark;
* one has three routes to Manhattan, the other has one that strands you when
  it is cancelled.

So each ZIP is scored on five named components, each 0-100 and individually
inspectable, combined with tunable weights.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping

from .geo import ZipIndex
from .gtfs import Network
from .raptor import StopProfile


@dataclass
class Weights:
    """Relative importance of each component. Need not sum to 1."""

    travel_time: float = 0.40
    transfers: float = 0.20
    frequency: float = 0.20
    redundancy: float = 0.10
    express: float = 0.10

    def total(self) -> float:
        return (
            self.travel_time + self.transfers + self.frequency + self.redundancy + self.express
        )


@dataclass
class ScoringConfig:
    """Thresholds that define what "good" means. All times in minutes."""

    #: Anything at or under this is a perfect travel-time score.
    ideal_minutes: int = 25
    #: The commute budget. At or beyond this, travel-time score is 0.
    budget_minutes: int = 60
    #: Peak window used for the frequency count, as hours after midnight.
    peak_start_hour: float = 6.0
    peak_end_hour: float = 9.5
    #: Departures per hour that count as "turn up and go".
    ideal_departures_per_hour: float = 8.0
    #: Distinct routes at which redundancy is considered fully satisfied.
    ideal_route_count: int = 3
    weights: Weights = field(default_factory=Weights)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass
class ZipScore:
    """One ZIP code's commute profile and score."""

    zip_code: str
    score: float
    best_minutes: int
    median_minutes: int
    transfers: int
    one_seat_ride: bool
    departures_per_hour: float
    route_count: int
    has_express: bool
    modes: list[str]
    stop_count: int
    #: Fraction of this ZIP's stops that reach Midtown within budget. A low
    #: value means only one corner of the ZIP is genuinely commutable.
    coverage: float
    best_stop_name: str
    routes: list[str]
    components: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        seat = "1-seat" if self.one_seat_ride else f"{self.transfers} transfer(s)"
        express = " express" if self.has_express else ""
        return (
            f"{self.zip_code}  {self.score:5.1f}  {self.best_minutes:3d} min  "
            f"{seat:12} {self.departures_per_hour:4.1f}/hr  "
            f"{self.route_count} routes{express}  {self.best_stop_name}"
        )


def score_zips(
    net: Network,
    profiles: Mapping[int, StopProfile],
    zip_index: ZipIndex,
    config: ScoringConfig | None = None,
    restrict_to: Iterable[str] | None = None,
) -> list[ZipScore]:
    """Aggregate stop profiles into ranked :class:`ZipScore` rows.

    ``profiles`` comes from :func:`househunt.commute.raptor.profile`.
    """
    cfg = config or ScoringConfig()
    allowed = set(restrict_to) if restrict_to else None
    peak_hours = max(cfg.peak_end_hour - cfg.peak_start_hour, 0.5)
    peak_lo = int(cfg.peak_start_hour * 3600)
    peak_hi = int(cfg.peak_end_hour * 3600)

    # Bucket every stop by ZIP, whether or not it is reachable, so coverage
    # can be measured against the ZIP's whole stop inventory.
    stops_by_zip: dict[str, list[int]] = {}
    for idx, stop in enumerate(net.stops):
        zip_code = zip_index.lookup(stop.lat, stop.lon)
        if zip_code is None:
            continue  # outside NJ (Manhattan targets, NYC feeder stops)
        if allowed is not None and zip_code not in allowed:
            continue
        stops_by_zip.setdefault(zip_code, []).append(idx)

    results: list[ZipScore] = []
    for zip_code, stop_indexes in stops_by_zip.items():
        reachable = [
            profiles[i] for i in stop_indexes if i in profiles
        ]
        if not reachable:
            continue

        within_budget = [p for p in reachable if p.best_minutes <= cfg.budget_minutes]
        if not within_budget:
            continue

        # The best stop is what matters for "should I look here" -- you would
        # buy near it. Median across the ZIP's usable stops is reported too,
        # so a ZIP that only just clips a station is visible as such.
        best = min(within_budget, key=lambda p: (p.best_seconds, p.min_trips))
        medians = sorted(p.median_minutes for p in within_budget)
        median_minutes = medians[len(medians) // 2]

        peak_departures = [d for d in best.departures if peak_lo <= d <= peak_hi]
        departures_per_hour = len(peak_departures) / peak_hours

        route_ids = sorted({r for p in within_budget for r in p.routes})
        routes = [net.routes[r] for r in route_ids if r in net.routes]
        has_express = any(r.is_express for r in routes)
        modes = sorted({r.mode for r in routes})

        # -- components ---------------------------------------------------
        span = max(cfg.budget_minutes - cfg.ideal_minutes, 1)
        time_score = _clamp(100.0 * (cfg.budget_minutes - best.best_minutes) / span)

        transfer_score = {0: 100.0, 1: 55.0, 2: 20.0}.get(best.transfers, 0.0)

        freq_score = _clamp(100.0 * departures_per_hour / cfg.ideal_departures_per_hour)

        redundancy_score = _clamp(100.0 * len(route_ids) / max(cfg.ideal_route_count, 1))

        # Rail and light rail are inherently more reliable than a bus stuck in
        # the same Lincoln Tunnel traffic as everyone else; an express bus
        # closes much of that gap.
        express_score = 0.0
        if has_express:
            express_score += 60.0
        if {"rail", "subway", "light_rail"} & set(modes):
            express_score += 40.0
        express_score = _clamp(express_score)

        w = cfg.weights
        components = {
            "travel_time": time_score,
            "transfers": transfer_score,
            "frequency": freq_score,
            "redundancy": redundancy_score,
            "express": express_score,
        }
        total = (
            time_score * w.travel_time
            + transfer_score * w.transfers
            + freq_score * w.frequency
            + redundancy_score * w.redundancy
            + express_score * w.express
        ) / (w.total() or 1.0)

        results.append(
            ZipScore(
                zip_code=zip_code,
                score=round(total, 1),
                best_minutes=best.best_minutes,
                median_minutes=median_minutes,
                transfers=best.transfers,
                one_seat_ride=best.one_seat_ride,
                departures_per_hour=round(departures_per_hour, 1),
                route_count=len(route_ids),
                has_express=has_express,
                modes=modes,
                stop_count=len(stop_indexes),
                coverage=round(len(within_budget) / len(stop_indexes), 3),
                best_stop_name=net.stops[best.stop_index].name,
                routes=[r.label for r in routes][:12],
                components={k: round(v, 1) for k, v in components.items()},
            )
        )

    results.sort(key=lambda z: (-z.score, z.best_minutes, z.zip_code))
    return results


def to_csv(scores: Iterable[ZipScore]) -> str:
    """Render scores as CSV -- the handoff format into the listing search."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "zip_code", "score", "best_minutes", "median_minutes", "transfers",
            "one_seat_ride", "departures_per_hour", "route_count", "has_express",
            "modes", "stop_count", "coverage", "best_stop_name", "routes",
        ]
    )
    for z in scores:
        writer.writerow(
            [
                z.zip_code, z.score, z.best_minutes, z.median_minutes, z.transfers,
                int(z.one_seat_ride), z.departures_per_hour, z.route_count,
                int(z.has_express), "|".join(z.modes), z.stop_count, z.coverage,
                z.best_stop_name, "|".join(z.routes),
            ]
        )
    return buf.getvalue()
