"""Feed registry, downloads, and Midtown target resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .geo import haversine_m
from .gtfs import Network


@dataclass
class FeedSpec:
    key: str
    label: str
    url: str | None
    #: True when the publisher requires a registered account to download.
    needs_account: bool = False
    notes: str = ""


#: Feeds worth loading for a NJ -> Midtown commute study.
#:
#: NJ Transit gates its GTFS behind a free developer account
#: (https://www.njtransit.com/developer-tools -> register -> accept the
#: licence), so those three cannot be fetched unattended. Download them once
#: and pass the paths in with --feed.
FEEDS = [
    FeedSpec("njt_bus", "NJ Transit bus", None, True,
             "developer.njtransit.com; the statewide bus feed, ~4M stop_times"),
    FeedSpec("njt_rail", "NJ Transit rail", None, True,
             "commuter rail; the backbone of any Midtown commute"),
    FeedSpec("njt_lightrail", "NJ Transit light rail", None, True,
             "Hudson-Bergen, Newark City Subway, RiverLINE"),
    FeedSpec("path", "PATH", "https://data.trilliumtransit.com/gtfs/path-nj-us/path-nj-us.zip",
             False, "Hoboken/Newark/JC into 33rd St and WTC"),
    FeedSpec("mta_subway", "MTA subway",
             "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip", False,
             "optional: only matters for the last mile past Penn/PABT"),
]

FEEDS_BY_KEY = {f.key: f for f in FEEDS}


#: Midtown destinations. Buses terminate at the Port Authority, trains at Penn,
#: and PATH at 33rd St -- scoring against only one of them badly misjudges
#: whole categories of town.
MIDTOWN_ANCHORS = {
    "ny_penn": (40.7506, -73.9935),
    "port_authority": (40.7570, -73.9903),
    "path_33rd": (40.7490, -73.9880),
}

#: Downtown, for a second run if the job is in the Financial District.
DOWNTOWN_ANCHORS = {
    "wtc_path": (40.7126, -74.0099),
    "fulton_st": (40.7101, -74.0079),
}


def download(spec: FeedSpec, dest_dir: str = "data", timeout: int = 300) -> str:
    """Fetch a public feed. Raises for feeds that need an account."""
    if spec.needs_account or not spec.url:
        raise RuntimeError(
            f"{spec.label} requires a registered developer account and cannot be "
            f"downloaded automatically. {spec.notes}"
        )
    import requests

    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"{spec.key}.zip")
    with requests.get(spec.url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
    return path


def resolve_targets(
    net: Network,
    anchors: dict[str, tuple[float, float]],
    radius_m: float = 400.0,
    egress_seconds: int = 300,
) -> list[tuple[int, int]]:
    """Find the network stops that represent each Midtown anchor.

    Matching by coordinate rather than by stop ID keeps this working across
    feeds that name the same terminal differently ("New York Penn Station",
    "NY PENN STATION", "PENN STATION NEW YORK").

    ``egress_seconds`` is the walk from the platform to a desk -- the default
    5 minutes is deliberate: a commute that ends at the Penn Station platform
    is not actually over.
    """
    targets: list[tuple[int, int]] = []
    for lat, lon in anchors.values():
        for idx, stop in enumerate(net.stops):
            if haversine_m(lat, lon, stop.lat, stop.lon) <= radius_m:
                targets.append((idx, egress_seconds))
    # One stop can sit inside two anchors' radii; keep the cheapest egress.
    best: dict[int, int] = {}
    for idx, egress in targets:
        if idx not in best or egress < best[idx]:
            best[idx] = egress
    return sorted(best.items())
