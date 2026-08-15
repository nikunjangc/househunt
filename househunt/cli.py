"""Command line entry points.

Two stages, run in order:

    # 1. rank NJ ZIP codes by commute to Midtown
    python -m househunt.cli commute \
        --feed data/njt_rail.zip --feed data/njt_bus.zip --feed data/path.zip \
        --budget 60 --out data/zip_scores.csv

    # 2. search the winning ZIPs for houses
    python -m househunt.cli listings \
        --scores data/zip_scores.csv --top 25 \
        --min-price 500000 --max-price 850000 --min-beds 2 \
        --reso-url https://api.example-mls.com/reso/odata --reso-token "$MLS_TOKEN" \
        --out data/houses.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

from .commute.feeds import DOWNTOWN_ANCHORS, MIDTOWN_ANCHORS, resolve_targets
from .commute.geo import ZipIndex
from .commute.gtfs import Feed, build_network
from .commute.raptor import build_footpaths, profile
from .commute.score import ScoringConfig, Weights, score_zips, to_csv

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _hhmm(value: str) -> int:
    """'08:45' -> seconds after midnight."""
    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected HH:MM, got {value!r}")
    return int(parts[0]) * 3600 + int(parts[1]) * 60


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# commute
# ---------------------------------------------------------------------------


def cmd_commute(args: argparse.Namespace) -> int:
    if not args.feed:
        _log("no --feed given; see README for how to obtain the NJ Transit feeds")
        return 2

    feeds = []
    for path in args.feed:
        if not os.path.exists(path):
            _log(f"missing feed: {path}")
            return 2
        started = time.monotonic()
        feed = Feed.from_zip(path)
        feeds.append(feed)
        _log(
            f"loaded {os.path.basename(path)}: {len(feed.stops):,} stops, "
            f"{len(feed.trips):,} trips ({time.monotonic() - started:.1f}s)"
        )

    weekday = WEEKDAYS.index(args.weekday.lower())
    net = build_network(feeds, service_weekday=weekday,
                        modes=args.modes.split(",") if args.modes else None)
    _log(f"network: {net.describe()}")
    if not net.patterns:
        _log("no service found for that weekday -- is the feed expired?")
        return 1

    build_footpaths(net, max_walk_m=args.max_walk)
    _log(f"footpaths: {sum(len(f) for f in net.footpaths):,} links "
         f"within {args.max_walk:.0f}m")

    anchors = dict(MIDTOWN_ANCHORS)
    if args.include_downtown:
        anchors.update(DOWNTOWN_ANCHORS)
    targets = resolve_targets(net, anchors, radius_m=args.anchor_radius,
                              egress_seconds=args.egress * 60)
    if not targets:
        _log("no stops found near the Manhattan anchors -- is a NY-side feed loaded?")
        return 1
    _log(f"targets: {len(targets)} stops near {len(anchors)} Manhattan anchors")

    deadlines = range(args.window_start, args.window_end + 1, args.step * 60)
    started = time.monotonic()
    profiles = profile(net, targets, deadlines, max_rounds=args.max_rounds,
                       max_travel_seconds=args.budget * 60)
    _log(f"routed {len(profiles):,} reachable stops over {len(list(deadlines))} "
         f"arrival times ({time.monotonic() - started:.1f}s)")

    zip_index = ZipIndex.from_geojson(args.zips)
    config = ScoringConfig(
        ideal_minutes=args.ideal,
        budget_minutes=args.budget,
        weights=Weights(
            travel_time=args.w_time, transfers=args.w_transfers,
            frequency=args.w_frequency, redundancy=args.w_redundancy,
            express=args.w_express,
        ),
    )
    scores = score_zips(net, profiles, zip_index, config)
    _log(f"scored {len(scores)} ZIP codes within a {args.budget}-minute budget")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(to_csv(scores))
        _log(f"wrote {args.out}")

    print(f"\n{'ZIP':6} {'SCORE':>5} {'BEST':>5} {'MED':>4} {'XFER':>4} "
          f"{'DEP/HR':>6} {'RTE':>3}  {'MODES':14} STATION")
    for z in scores[: args.top]:
        print(f"{z.zip_code:6} {z.score:5.1f} {z.best_minutes:5d} {z.median_minutes:4d} "
              f"{z.transfers:4d} {z.departures_per_hour:6.1f} {z.route_count:3d}  "
              f"{'/'.join(z.modes)[:14]:14} {z.best_stop_name[:34]}")
    return 0


# ---------------------------------------------------------------------------
# listings
# ---------------------------------------------------------------------------


def cmd_listings(args: argparse.Namespace) -> int:
    from .listings.search import ConcurrentSearch, SearchCriteria, dedupe

    zips: list[str] = []
    commute_by_zip: dict[str, tuple[int, int]] = {}
    if args.scores:
        with open(args.scores, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                zips.append(row["zip_code"])
                commute_by_zip[row["zip_code"]] = (
                    int(row["best_minutes"]), int(row["transfers"])
                )
        zips = zips[: args.top]
    zips.extend(z for z in args.zip or [] if z not in zips)
    if not zips:
        _log("no ZIP codes: pass --scores from the commute stage, or --zip")
        return 2

    if args.reso_url:
        from .listings.sources import ResoWebApiSource

        token = args.reso_token or os.environ.get("MLS_TOKEN", "")
        if not token:
            _log("--reso-url needs --reso-token (or the MLS_TOKEN env var)")
            return 2
        source = ResoWebApiSource(args.reso_url, token)
    elif args.njmls:
        from .listings.sources import NjmlsPortalSource

        source = NjmlsPortalSource(acknowledge_terms=True)
        _log("using the njmls.com portal; keep --rate low and see the README "
             "on terms of use")
    else:
        _log("choose a backend: --reso-url (licensed feed) or --njmls (portal)")
        return 2

    criteria = SearchCriteria(
        min_price=args.min_price, max_price=args.max_price,
        min_beds=args.min_beds, min_baths=args.min_baths,
        limit_per_zip=args.limit_per_zip,
    )
    _log(f"searching {len(zips)} ZIPs for {criteria.describe()}")

    search = ConcurrentSearch(
        source, max_workers=args.workers, requests_per_second=args.rate,
        on_progress=lambda r: _log(
            f"  {r.zip_code}: {len(r.listings)} listings" if r.ok
            else f"  {r.zip_code}: FAILED {r.error}"
        ),
    )
    results = search.run(zips, criteria, commute_by_zip=commute_by_zip)
    listings = dedupe(results.listings)
    _log(results.summary() + f"; {len(listings)} after dedupe")

    listings.sort(key=lambda l: (
        l.commute_minutes if l.commute_minutes is not None else 10**6,
        l.list_price if l.list_price is not None else 10**9,
    ))

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["zip", "commute_min", "transfers", "price", "beds",
                             "baths", "sq_ft", "address", "city", "mls_id", "url"])
            for l in listings:
                h = l.house
                writer.writerow([h.zip_code, l.commute_minutes, l.commute_transfers,
                                 l.list_price, h.beds, h.baths, h.sq_ft,
                                 h.street_address, h.city, l.mls_id, l.url])
        _log(f"wrote {args.out}")

    for l in listings[: args.top_listings]:
        price = f"${l.list_price:,}" if l.list_price else "n/a"
        print(f"{l.house.zip_code}  {str(l.commute_minutes or '?'):>3} min  "
              f"{price:>10}  {l.house.beds or '?'}bd  {l.house.street_address}, "
              f"{l.house.city}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="househunt", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("commute", help="rank NJ ZIP codes by commute to Midtown")
    c.add_argument("--feed", action="append", metavar="PATH",
                   help="GTFS zip; repeat for each feed")
    c.add_argument("--zips", default="data/nj_zips.geojson", help="ZCTA GeoJSON")
    c.add_argument("--budget", type=int, default=60, help="commute budget, minutes")
    c.add_argument("--ideal", type=int, default=25, help="a perfect commute, minutes")
    c.add_argument("--window-start", type=_hhmm, default=_hhmm("07:00"),
                   metavar="HH:MM", help="earliest arrival considered")
    c.add_argument("--window-end", type=_hhmm, default=_hhmm("09:30"),
                   metavar="HH:MM", help="latest arrival considered")
    c.add_argument("--step", type=int, default=5,
                   help="minutes between arrival times sampled")
    c.add_argument("--weekday", default="wednesday", choices=WEEKDAYS)
    c.add_argument("--max-walk", type=float, default=500.0,
                   help="metres between walk-connected stops")
    c.add_argument("--max-rounds", type=int, default=4,
                   help="max vehicle trips (rounds); 4 allows 3 transfers")
    c.add_argument("--egress", type=int, default=5,
                   help="minutes from the arrival platform to the desk")
    c.add_argument("--anchor-radius", type=float, default=400.0,
                   help="metres around each Manhattan anchor")
    c.add_argument("--include-downtown", action="store_true",
                   help="also target WTC/Fulton St")
    c.add_argument("--modes", help="comma-separated filter, e.g. rail,bus,light_rail")
    c.add_argument("--top", type=int, default=40, help="rows to print")
    c.add_argument("--out", default="data/zip_scores.csv")
    for name, default in (("time", 0.40), ("transfers", 0.20), ("frequency", 0.20),
                          ("redundancy", 0.10), ("express", 0.10)):
        c.add_argument(f"--w-{name}", type=float, default=default,
                       help=f"weight for {name} (default {default})")
    c.set_defaults(func=cmd_commute)

    l = sub.add_parser("listings", help="search houses across the best ZIPs")
    l.add_argument("--scores", help="zip_scores.csv from the commute stage")
    l.add_argument("--zip", action="append", help="explicit ZIP; repeatable")
    l.add_argument("--top", type=int, default=25, help="how many ZIPs to search")
    l.add_argument("--min-price", type=int, default=500_000)
    l.add_argument("--max-price", type=int, default=850_000)
    l.add_argument("--min-beds", type=int, default=2)
    l.add_argument("--min-baths", type=float)
    l.add_argument("--limit-per-zip", type=int, default=250)
    l.add_argument("--workers", type=int, default=6)
    l.add_argument("--rate", type=float, default=1.0, help="requests per second")
    l.add_argument("--reso-url", help="RESO Web API service root")
    l.add_argument("--reso-token", help="bearer token (or set MLS_TOKEN)")
    l.add_argument("--njmls", action="store_true", help="use the njmls.com portal")
    l.add_argument("--top-listings", type=int, default=50)
    l.add_argument("--out", default="data/houses.csv")
    l.set_defaults(func=cmd_listings)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
