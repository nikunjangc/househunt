"""Arrive-by routing over a GTFS network.

Why not plain Dijkstra
----------------------
Dijkstra assumes a fixed cost per edge. On a transit network the cost of
"ride the 159 from Fort Lee to Port Authority" depends entirely on *when you
show up*: at 07:40 it is a 5-minute wait, at 10:40 it is a 55-minute wait.
Modelling that with static edges either ignores waiting (wildly optimistic) or
bakes in an average (wrong in both directions, and it silently erases the
difference between a 5-minute-headway express and a 3-buses-a-day commuter
run -- exactly the distinction we are trying to measure).

The correct formulation is a *time-dependent* shortest path. This module
implements RAPTOR (Delling, Pajor & Werneck), which solves it by rounds
instead of by a priority queue: after round *k*, every stop holds the best
journey using at most *k* vehicle trips. That falls out of the algorithm for
free and is precisely the "one bus vs. two bus" signal we want to score on.

The search runs *backwards* from Midtown: labels are the **latest departure**
from each stop that still reaches the destination by the deadline. One
backward pass yields every origin at once, which is what makes scoring ~600
ZIP codes tractable.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from .geo import haversine_m, deg_box
from .gtfs import Network

NEVER = -(10**9)

#: Brisk but realistic walking speed, m/s (~4.8 km/h).
WALK_SPEED_MPS = 1.33
#: Fixed cost of any transfer: platform change, fare gate, and the fact that
#: nobody sprints for a connection with zero slack.
TRANSFER_PENALTY_S = 120
#: Stops closer than this are treated as walkable connections.
DEFAULT_MAX_WALK_M = 500.0


@dataclass
class StopLabel:
    """Best arrive-by journey found for one stop."""

    departure: int
    """Latest departure time from this stop, seconds after midnight."""
    trips: int
    """Number of vehicle trips used (so ``trips - 1`` transfers)."""
    travel_seconds: int
    """Deadline minus departure: total door-to-door time including waiting."""
    first_route: str | None = None
    """Route ID of the first vehicle boarded, for express/mode reporting."""


def build_footpaths(
    net: Network,
    max_walk_m: float = DEFAULT_MAX_WALK_M,
    same_station_seconds: int = 60,
) -> None:
    """Populate ``net.footpaths`` with walking links between nearby stops.

    Uses a uniform grid so this stays near-linear; a statewide bus feed has
    ~20k stops and the all-pairs alternative is 400M distance calls.
    """
    cell_m = max_walk_m
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)

    def cell_of(lat: float, lon: float) -> tuple[int, int]:
        # Degenerate-but-fine equirectangular binning over a state-sized area.
        return (int(lat * 111_320.0 // cell_m), int(lon * 84_000.0 // cell_m))

    for idx, stop in enumerate(net.stops):
        grid[cell_of(stop.lat, stop.lon)].append(idx)

    links: list[list[tuple[int, int]]] = [[] for _ in net.stops]
    for idx, stop in enumerate(net.stops):
        cx, cy = cell_of(stop.lat, stop.lon)
        min_lat, min_lon, max_lat, max_lon = deg_box(stop.lat, stop.lon, max_walk_m)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((cx + dx, cy + dy), ()):
                    if other == idx:
                        continue
                    o = net.stops[other]
                    if not (min_lat <= o.lat <= max_lat and min_lon <= o.lon <= max_lon):
                        continue
                    dist = haversine_m(stop.lat, stop.lon, o.lat, o.lon)
                    if dist > max_walk_m:
                        continue
                    # Platforms of one station connect faster than a street
                    # crossing between two unrelated stops.
                    shared_parent = (
                        stop.parent_station is not None
                        and stop.parent_station == o.parent_station
                    )
                    seconds = (
                        same_station_seconds
                        if shared_parent
                        else int(dist / WALK_SPEED_MPS) + TRANSFER_PENALTY_S
                    )
                    links[idx].append((other, seconds))

    net.footpaths = links


def _latest_trip_arriving_by(pattern, position: int, limit: int) -> int | None:
    """Index of the latest trip whose arrival at ``position`` is <= ``limit``.

    Trips on a pattern are sorted by departure and assumed not to overtake, so
    arrivals at a fixed position are sorted too and this is a binary search.
    """
    arrivals = pattern.trip_arrivals
    lo, hi = 0, len(arrivals)
    while lo < hi:
        mid = (lo + hi) // 2
        if arrivals[mid][position] <= limit:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo > 0 else None


def arrive_by(
    net: Network,
    targets: Sequence[tuple[int, int]],
    deadline: int,
    max_rounds: int = 5,
    max_travel_seconds: int | None = None,
) -> dict[int, StopLabel]:
    """Backward RAPTOR: latest departure from every stop reaching a target.

    ``targets`` are ``(stop index, egress seconds)`` pairs -- the Midtown
    anchors plus the walk from the platform to the desk. ``deadline`` is the
    arrival time to be at work by, in seconds after midnight.

    Returns a label per reachable stop. Unreachable stops are simply absent.
    """
    n = len(net.stops)
    # best[p] is the latest known departure from p over all rounds; it doubles
    # as RAPTOR's local pruning bound.
    best = [NEVER] * n
    best_trips = [0] * n
    best_route: list[str | None] = [None] * n
    prev = [NEVER] * n  # labels from round k-1
    cutoff = deadline - max_travel_seconds if max_travel_seconds else None

    marked: set[int] = set()
    for stop_idx, egress in targets:
        if not (0 <= stop_idx < n):
            continue
        value = deadline - egress
        if value > prev[stop_idx]:
            prev[stop_idx] = value
            best[stop_idx] = value
            marked.add(stop_idx)

    # Walking to a target also counts as arriving (round 0).
    for stop_idx in list(marked):
        for other, seconds in net.footpaths[stop_idx]:
            value = prev[stop_idx] - seconds
            if value > prev[other]:
                prev[other] = value
                best[other] = value
                marked.add(other)

    for round_no in range(1, max_rounds + 1):
        if not marked:
            break

        # Every pattern touched by a marked stop needs scanning, starting from
        # the furthest-along position that was marked.
        queue: dict[int, int] = {}
        for stop_idx in marked:
            for p_idx, pos in net.stop_patterns[stop_idx]:
                if pos > queue.get(p_idx, -1):
                    queue[p_idx] = pos

        marked = set()
        current = [NEVER] * n

        for p_idx, start_pos in queue.items():
            pattern = net.patterns[p_idx]
            trip: int | None = None
            departures = pattern.trip_departures

            # Walk the pattern against the direction of travel. Riding a
            # vehicle backwards from an alighting point gives us the latest
            # departure at every earlier stop on that trip.
            for pos in range(start_pos, -1, -1):
                stop_idx = pattern.stops[pos]

                if trip is not None:
                    dep = departures[trip][pos]
                    if dep > best[stop_idx] and (cutoff is None or dep >= cutoff):
                        best[stop_idx] = dep
                        current[stop_idx] = dep
                        best_trips[stop_idx] = round_no
                        best_route[stop_idx] = pattern.route_id
                        marked.add(stop_idx)

                # Can we alight here off a *later* trip? That would let us
                # depart every earlier stop later, which is strictly better.
                limit = prev[stop_idx]
                if limit != NEVER:
                    candidate = _latest_trip_arriving_by(pattern, pos, limit)
                    if candidate is not None and (trip is None or candidate > trip):
                        trip = candidate

        # Relax footpaths over stops improved this round.
        for stop_idx in list(marked):
            base = current[stop_idx]
            for other, seconds in net.footpaths[stop_idx]:
                value = base - seconds
                if value > best[other] and (cutoff is None or value >= cutoff):
                    best[other] = value
                    current[other] = value
                    best_trips[other] = round_no
                    best_route[other] = best_route[stop_idx]
                    marked.add(other)

        prev = current

    labels: dict[int, StopLabel] = {}
    for idx in range(n):
        if best[idx] == NEVER or best_trips[idx] == 0:
            # best_trips == 0 means the target itself (or a walk to it); it is
            # not a commute origin worth reporting.
            continue
        labels[idx] = StopLabel(
            departure=best[idx],
            trips=best_trips[idx],
            travel_seconds=deadline - best[idx],
            first_route=best_route[idx],
        )
    return labels


@dataclass
class StopProfile:
    """Aggregated arrive-by results for one stop across a peak window."""

    stop_index: int
    best_seconds: int
    median_seconds: int
    min_trips: int
    departures: list[int]
    routes: set[str]

    @property
    def best_minutes(self) -> int:
        return round(self.best_seconds / 60)

    @property
    def median_minutes(self) -> int:
        return round(self.median_seconds / 60)

    @property
    def transfers(self) -> int:
        return max(self.min_trips - 1, 0)

    @property
    def one_seat_ride(self) -> bool:
        return self.min_trips == 1


def profile(
    net: Network,
    targets: Sequence[tuple[int, int]],
    deadlines: Iterable[int],
    max_rounds: int = 5,
    max_travel_seconds: int | None = None,
) -> dict[int, StopProfile]:
    """Run :func:`arrive_by` at several deadlines and aggregate per stop.

    A single arrive-by run says "you can make the 07:12". Sweeping the arrival
    window shows how *many* distinct departures work, which is the difference
    between a station with a train every 8 minutes and one with three trains a
    morning -- both of which a single run would report identically.
    """
    runs: dict[int, list[StopLabel]] = defaultdict(list)
    for deadline in deadlines:
        for idx, label in arrive_by(
            net,
            targets,
            deadline,
            max_rounds=max_rounds,
            max_travel_seconds=max_travel_seconds,
        ).items():
            runs[idx].append(label)

    profiles: dict[int, StopProfile] = {}
    for idx, labels in runs.items():
        times = sorted(l.travel_seconds for l in labels)
        mid = len(times) // 2
        median = times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) // 2
        profiles[idx] = StopProfile(
            stop_index=idx,
            best_seconds=times[0],
            median_seconds=median,
            min_trips=min(l.trips for l in labels),
            # Distinct usable departures are the frequency signal.
            departures=sorted({l.departure for l in labels}),
            routes={l.first_route for l in labels if l.first_route},
        )
    return profiles
