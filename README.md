# househunt

Find NJ ZIP codes that are genuinely commutable to Midtown Manhattan, then
search for houses in them.

Two stages:

1. **commute** — build a transit graph from GTFS feeds (NJ Transit rail, bus and
   light rail, PATH, optionally MTA), run an arrive-by shortest-path search from
   every stop in New Jersey to Midtown, and score every ZIP code on travel time,
   transfers, frequency, route redundancy and express service.
2. **listings** — take the winning ZIPs and search them concurrently for houses
   in your price and bedroom range.

## Why RAPTOR and not plain Dijkstra

Dijkstra assumes a fixed cost per edge. On a transit network the cost of "ride
the 159 to Port Authority" depends entirely on when you show up: at 07:40 it is
a five-minute wait, at 10:40 it is fifty-five. Modelling that with static edges
either ignores waiting (wildly optimistic) or bakes in an average — which
silently erases the difference between a train every eight minutes and three
buses a morning. That difference is most of what you are actually trying to
measure.

The correct formulation is a *time-dependent* shortest path. This uses
[RAPTOR](https://www.microsoft.com/en-us/research/publication/round-based-public-transit-routing/)
(Delling, Pajor & Werneck), which solves it in rounds rather than with a
priority queue. After round *k*, every stop holds the best journey using at most
*k* vehicle trips — so "one bus vs. two bus" falls out of the algorithm for free
rather than needing to be reconstructed afterwards.

The search runs **backwards** from Midtown: each label is the latest departure
from a stop that still gets you there by the deadline. One backward pass scores
every origin in the state at once, which is what makes ~600 ZIP codes tractable.
Sweeping the deadline across the morning peak (`--window-start`/`--window-end`)
turns a single answer into a profile, which is where the frequency numbers come
from.

Scoring is in `househunt/commute/score.py` and every component is exposed
individually, so you can see *why* a ZIP ranked where it did and re-weight it:

| component | what it measures |
|---|---|
| `travel_time` | best door-to-Midtown time, including waiting and a 5-min egress walk |
| `transfers` | one-seat ride vs. one, two, three transfers |
| `frequency` | usable departures per hour in the AM peak |
| `redundancy` | how many distinct routes serve the ZIP — one cancelled bus should not strand you |
| `express` | express/limited bus service, and a bonus for rail over bus |

## Install

```bash
pip install -e .
```

Only `requests` is needed for the commute stage; `tinydb` is optional and used
by the listing cache.

## Stage 1: commute

### Getting the feeds

| feed | source | account needed |
|---|---|---|
| NJ Transit bus | [njtransit.com/developer-tools](https://www.njtransit.com/developer-tools) | yes — free registration |
| NJ Transit rail | same | yes |
| NJ Transit light rail | same | yes |
| PATH | `https://data.trilliumtransit.com/gtfs/path-nj-us/path-nj-us.zip` | no |
| MTA subway | `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip` | no |

**NJ Transit gates its GTFS behind a free developer account** — register,
accept the licence, and download the bus, rail and light-rail zips by hand.
There is no unauthenticated URL, so this cannot be automated for you.

The ZIP boundary file (`data/nj_zips.geojson`, all 595 NJ ZCTAs) is committed,
so nothing else is needed.

### Run it

```bash
python -m househunt.cli commute \
    --feed data/njt_rail.zip \
    --feed data/njt_bus.zip \
    --feed data/njt_lightrail.zip \
    --feed data/path.zip \
    --budget 60 \
    --window-start 07:00 --window-end 09:30 --step 5 \
    --out data/zip_scores.csv
```

```
ZIP    SCORE  BEST  MED XFER DEP/HR RTE  MODES          STATION
07030   78.8    20   22    0    4.6   1  subway         Hoboken PATH
07094   70.9    20   35    0    1.4   1  rail           Secaucus Junction
07024   65.8    40   45    0    2.3   1  bus            Fort Lee Main St
```

Useful flags:

- `--budget 60` — the commute ceiling in minutes; anything slower is dropped.
- `--egress 5` — minutes from the arrival platform to your desk. A commute that
  ends on the Penn Station platform is not actually over.
- `--max-rounds 4` — cap on vehicle trips; `4` allows up to three transfers.
- `--modes rail,light_rail` — score rail-only options.
- `--include-downtown` — also target WTC/Fulton St instead of just Midtown.
- `--w-time`, `--w-transfers`, `--w-frequency`, `--w-redundancy`, `--w-express` —
  re-weight the score without re-running the routing.

Destinations default to three Midtown anchors — **NY Penn, Port Authority Bus
Terminal, and 33rd St PATH**. Scoring against Penn alone badly misjudges whole
categories of town, because most NJ commuter buses terminate at Port Authority
and never touch Penn.

## Stage 2: listings

```bash
python -m househunt.cli listings \
    --scores data/zip_scores.csv --top 25 \
    --min-price 500000 --max-price 850000 --min-beds 2 \
    --reso-url https://api.your-mls.com/reso/odata --reso-token "$MLS_TOKEN" \
    --out data/houses.csv
```

Results come back sorted by commute, then price, with each listing carrying the
commute minutes and transfer count for its ZIP.

### Two backends

**`ResoWebApiSource` (recommended).** NJMLS distributes listing data to licensed
IDX participants as a feed, through
[RE/Advantage](https://www.newjerseymls.com/services/internet-data-exchange-idx/).
Modern MLS feeds speak the [RESO Web API](https://www.reso.org/reso-web-api/) —
OData over HTTPS with a bearer token. If you or your agent can get feed
credentials, this needs no scraping at all, and because it uses RESO Data
Dictionary field names the same code works against most other MLSs.

**`NjmlsPortalSource`.** Queries the public njmls.com search the way a browser
does: establishes one session, reuses it across ZIPs, throttles, and pages.
Listing data on the portal is IDX-licensed and njmls.com's terms govern
automated access, so the class refuses to run until you pass
`acknowledge_terms=True` and it checks `robots.txt` first (and refuses if it
cannot read it, rather than guessing). Keep `--rate` low and prefer the feed.

Its HTML parser tries embedded JSON-LD, which is far more stable than matching
CSS classes — but **it has not been validated against a live response**, because
njmls.com is unreachable from the environment this was built in. If it returns
nothing, capture one real search response and fit a parser to it:

```python
NjmlsPortalSource(acknowledge_terms=True, parse_html=my_parser)
```

The form field names in `NjmlsEndpoint.field_names` need the same treatment —
check them against a real search POST in browser devtools.

## Layout

```
househunt/
  cli.py              # both stages
  commute/
    geo.py            # haversine, ZCTA point-in-polygon (grid-indexed)
    gtfs.py           # feed parsing, multi-feed merge, trips -> RAPTOR patterns
    raptor.py         # backward RAPTOR, footpaths, peak-window profiles
    score.py          # per-ZIP scoring
    feeds.py          # feed registry, Midtown anchors, target resolution
  listings/
    models.py         # House / Listing / ListCache
    search.py         # concurrent multi-ZIP runner, rate limiting, dedupe
    sources.py        # RESO Web API and NJMLS portal backends
tests/                # 49 tests, no network required
```

Run the tests with `python -m unittest discover -s tests -t .`.

`tests/fixtures.py` builds a small real GTFS zip — a miniature North Jersey with
a rail line, a feeder bus, an express bus and a PATH line — whose timetable is
simple enough that every expected answer can be worked out on paper. The router
tests assert against those hand-computed values.

## History

This started as a Python 2 Redfin/Zillow scraper. The `House`, `Listing` and
`ListCache` model was worth keeping and was ported; the Zillow client (the ZWSID
API has been retired), the Redfin scraper, and the 550KB of generated XSD
bindings in `searchresults.py` were removed. See the git history for the
original.
