"""GTFS feed loading, merging, and conversion into a RAPTOR-ready network.

A GTFS ``route_id`` is not what RAPTOR calls a route. NJ Transit's 159 bus, for
instance, is one ``route_id`` covering many different stop sequences (short
turns, peak-only express variants, park-and-ride branches). RAPTOR needs
*patterns*: groups of trips that visit exactly the same stops in the same
order. :func:`build_network` does that grouping.

Feeds are merged with per-feed ID prefixes so NJ Transit bus stop "123" and
PATH stop "123" stay distinct, while stops that are physically the same place
get linked by the generated footpaths in :mod:`.raptor`.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

#: GTFS route_type -> coarse mode name. 3 is bus, 2 heavy rail, 0 tram/light
#: rail, 1 subway/metro, 4 ferry. NJ Transit uses 2 (rail), 3 (bus) and 0
#: (Hudson-Bergen Light Rail, RiverLINE, Newark Light Rail); PATH reports 1.
ROUTE_TYPE_NAMES = {
    0: "light_rail",
    1: "subway",
    2: "rail",
    3: "bus",
    4: "ferry",
    5: "cable_tram",
    6: "aerial_lift",
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
}

# Words operators put in route names for limited-stop / commuter-express
# service. Used to flag express service, which is a big quality-of-commute
# signal the raw travel time alone does not capture.
_EXPRESS_TOKENS = ("express", "xpress", "ltd", "limited", "peak", "commuter")


def parse_gtfs_time(value: str) -> int | None:
    """GTFS ``HH:MM:SS`` to seconds after midnight.

    Hours legitimately exceed 24 for trips that run past midnight on the
    prior service day (``25:10:00`` is 1:10am), so this must not wrap.
    """
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def fmt_time(seconds: int) -> str:
    """Inverse of :func:`parse_gtfs_time`, for human-readable output."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float
    feed: str
    parent_station: str | None = None


@dataclass
class Route:
    route_id: str
    short_name: str
    long_name: str
    route_type: int
    feed: str
    agency_id: str | None = None

    @property
    def mode(self) -> str:
        return ROUTE_TYPE_NAMES.get(self.route_type, f"type_{self.route_type}")

    @property
    def label(self) -> str:
        return self.short_name or self.long_name or self.route_id

    @property
    def is_express(self) -> bool:
        blob = f"{self.short_name} {self.long_name}".lower()
        return any(tok in blob for tok in _EXPRESS_TOKENS)


@dataclass
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    feed: str
    headsign: str = ""
    #: Parallel arrays over the trip's stop sequence.
    stop_ids: list[str] = field(default_factory=list)
    arrivals: list[int] = field(default_factory=list)
    departures: list[int] = field(default_factory=list)
    #: True where GTFS says the stop is drop-off-only / pick-up-only.
    pickup_allowed: list[bool] = field(default_factory=list)
    dropoff_allowed: list[bool] = field(default_factory=list)


class Feed:
    """One parsed GTFS zip (or unpacked directory)."""

    def __init__(self, name: str):
        self.name = name
        self.stops: dict[str, Stop] = {}
        self.routes: dict[str, Route] = {}
        self.trips: dict[str, Trip] = {}
        #: service_id -> set of weekday numbers (0=Mon) it runs on.
        self.service_days: dict[str, set[int]] = {}

    # -- loading ---------------------------------------------------------

    @classmethod
    def from_zip(cls, path: str, name: str | None = None) -> "Feed":
        feed = cls(name or _stem(path))
        with zipfile.ZipFile(path) as zf:
            names = {n.split("/")[-1]: n for n in zf.namelist()}

            def table(filename: str) -> Iterator[dict]:
                member = names.get(filename)
                if member is None:
                    return iter(())
                with zf.open(member) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                    yield from csv.DictReader(text)

            feed._load(table)
        return feed

    def _load(self, table) -> None:
        prefix = f"{self.name}:"

        for row in table("stops.txt"):
            # location_type 1 is a station (a container), 2/3/4 are entrances
            # and generic nodes. Only boardable platforms/stops matter here.
            if (row.get("location_type") or "0").strip() not in ("", "0"):
                continue
            try:
                lat = float(row["stop_lat"])
                lon = float(row["stop_lon"])
            except (KeyError, TypeError, ValueError):
                continue
            sid = prefix + row["stop_id"]
            parent = (row.get("parent_station") or "").strip()
            self.stops[sid] = Stop(
                stop_id=sid,
                name=(row.get("stop_name") or "").strip(),
                lat=lat,
                lon=lon,
                feed=self.name,
                parent_station=prefix + parent if parent else None,
            )

        for row in table("routes.txt"):
            try:
                rtype = int(row.get("route_type") or -1)
            except ValueError:
                rtype = -1
            rid = prefix + row["route_id"]
            self.routes[rid] = Route(
                route_id=rid,
                short_name=(row.get("route_short_name") or "").strip(),
                long_name=(row.get("route_long_name") or "").strip(),
                route_type=rtype,
                feed=self.name,
                agency_id=(row.get("agency_id") or "").strip() or None,
            )

        for row in table("trips.txt"):
            tid = prefix + row["trip_id"]
            self.trips[tid] = Trip(
                trip_id=tid,
                route_id=prefix + row["route_id"],
                service_id=prefix + row["service_id"],
                feed=self.name,
                headsign=(row.get("trip_headsign") or "").strip(),
            )

        # stop_times is by far the largest table (millions of rows for a
        # statewide bus feed), so it is streamed straight into the trips and
        # sorted once at the end rather than held as an intermediate list.
        seq_buf: dict[str, list[tuple[int, str, int, int, bool, bool]]] = defaultdict(list)
        for row in table("stop_times.txt"):
            tid = prefix + row["trip_id"]
            if tid not in self.trips:
                continue
            arr = parse_gtfs_time(row.get("arrival_time", ""))
            dep = parse_gtfs_time(row.get("departure_time", ""))
            if arr is None and dep is None:
                # Interpolated (blank) times: skipping keeps the timetable
                # honest rather than inventing stops we cannot time.
                continue
            arr = arr if arr is not None else dep
            dep = dep if dep is not None else arr
            try:
                seq = int(row.get("stop_sequence") or 0)
            except ValueError:
                seq = 0
            seq_buf[tid].append(
                (
                    seq,
                    prefix + row["stop_id"],
                    arr,
                    dep,
                    (row.get("pickup_type") or "0").strip() != "1",
                    (row.get("drop_off_type") or "0").strip() != "1",
                )
            )

        for tid, rows in seq_buf.items():
            rows.sort()
            trip = self.trips[tid]
            for _, sid, arr, dep, pick, drop in rows:
                if sid not in self.stops:
                    continue
                trip.stop_ids.append(sid)
                trip.arrivals.append(arr)
                trip.departures.append(dep)
                trip.pickup_allowed.append(pick)
                trip.dropoff_allowed.append(drop)

        # Drop trips that ended up with nothing usable.
        self.trips = {t: tr for t, tr in self.trips.items() if len(tr.stop_ids) >= 2}

        for row in table("calendar.txt"):
            sid = prefix + row["service_id"]
            days = {
                i
                for i, col in enumerate(
                    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
                )
                if (row.get(col) or "0").strip() == "1"
            }
            self.service_days[sid] = days

        # calendar_dates alone defines service in feeds with no calendar.txt
        # (exception_type 1 = added, 2 = removed).
        for row in table("calendar_dates.txt"):
            sid = prefix + row["service_id"]
            if (row.get("exception_type") or "").strip() != "1":
                continue
            date = (row.get("date") or "").strip()
            if len(date) != 8 or not date.isdigit():
                continue
            weekday = _weekday(int(date[:4]), int(date[4:6]), int(date[6:8]))
            self.service_days.setdefault(sid, set()).add(weekday)


def _stem(path: str) -> str:
    base = path.replace("\\", "/").split("/")[-1]
    return base[:-4] if base.lower().endswith(".zip") else base


def _weekday(y: int, m: int, d: int) -> int:
    """Weekday with 0=Monday, via Zeller-style arithmetic (no imports)."""
    import datetime as _dt

    return _dt.date(y, m, d).weekday()


# ---------------------------------------------------------------------------
# Network: patterns + indexes that RAPTOR scans
# ---------------------------------------------------------------------------


@dataclass
class Pattern:
    """Trips sharing one exact stop sequence, sorted by departure time."""

    stops: tuple[int, ...]
    route_id: str
    #: trips[t][i] timings at position i of trip t. Both are flat lists of
    #: length len(stops) so the scan can index them without object churn.
    trip_arrivals: list[list[int]] = field(default_factory=list)
    trip_departures: list[list[int]] = field(default_factory=list)
    trip_ids: list[str] = field(default_factory=list)

    @property
    def n_trips(self) -> int:
        return len(self.trip_ids)


class Network:
    """Everything the router needs, with stops interned to integer indexes."""

    def __init__(self) -> None:
        self.stop_ids: list[str] = []
        self.stop_index: dict[str, int] = {}
        self.stops: list[Stop] = []
        self.routes: dict[str, Route] = {}
        self.patterns: list[Pattern] = []
        #: stop index -> list of (pattern index, position within pattern)
        self.stop_patterns: list[list[tuple[int, int]]] = []
        #: stop index -> list of (stop index, walk seconds)
        self.footpaths: list[list[tuple[int, int]]] = []

    def intern(self, stop: Stop) -> int:
        idx = self.stop_index.get(stop.stop_id)
        if idx is None:
            idx = len(self.stop_ids)
            self.stop_index[stop.stop_id] = idx
            self.stop_ids.append(stop.stop_id)
            self.stops.append(stop)
            self.stop_patterns.append([])
            self.footpaths.append([])
        return idx

    def route_for_pattern(self, pattern_idx: int) -> Route | None:
        return self.routes.get(self.patterns[pattern_idx].route_id)

    def describe(self) -> str:
        trips = sum(p.n_trips for p in self.patterns)
        walks = sum(len(f) for f in self.footpaths)
        return (
            f"{len(self.stops):,} stops, {len(self.routes):,} routes, "
            f"{len(self.patterns):,} patterns, {trips:,} trips, {walks:,} footpaths"
        )


def build_network(
    feeds: Sequence[Feed],
    service_weekday: int = 2,
    modes: Iterable[str] | None = None,
) -> Network:
    """Fold feeds into a :class:`Network` for one weekday.

    ``service_weekday`` is 0=Monday; the default of 2 (Wednesday) is the
    conventional choice for a representative weekday timetable.
    """
    wanted_modes = set(modes) if modes else None
    net = Network()

    for feed in feeds:
        net.routes.update(feed.routes)

        # Group trips by (route, exact stop sequence) -> pattern.
        groups: dict[tuple[str, tuple[str, ...]], list[Trip]] = defaultdict(list)
        for trip in feed.trips.values():
            days = feed.service_days.get(trip.service_id)
            # A service_id with no calendar entry at all is unusable; an empty
            # day set means it simply does not run today.
            if not days or service_weekday not in days:
                continue
            route = feed.routes.get(trip.route_id)
            if route is None:
                continue
            if wanted_modes is not None and route.mode not in wanted_modes:
                continue
            groups[(trip.route_id, tuple(trip.stop_ids))].append(trip)

        for (route_id, stop_seq), trips in groups.items():
            indexes = tuple(net.intern(feed.stops[s]) for s in stop_seq)
            pattern = Pattern(stops=indexes, route_id=route_id)
            # Sort by departure from the first stop. RAPTOR's trip search
            # assumes trips on a pattern do not overtake one another.
            trips.sort(key=lambda t: t.departures[0])
            for trip in trips:
                pattern.trip_arrivals.append(trip.arrivals)
                pattern.trip_departures.append(trip.departures)
                pattern.trip_ids.append(trip.trip_id)

            p_idx = len(net.patterns)
            net.patterns.append(pattern)
            for pos, s_idx in enumerate(indexes):
                net.stop_patterns[s_idx].append((p_idx, pos))

    return net
