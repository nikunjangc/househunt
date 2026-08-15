"""A small hand-computable GTFS feed, written as a real zip.

Exercising the actual zip/CSV reader (rather than hand-building a Network)
keeps the parser and the router honest together.

Geography is a miniature North Jersey. Every timetable below is chosen so the
expected answers can be worked out on paper -- see ``test_raptor.py``.

    RIDGEWOOD --R1--> GLEN_ROCK --R1--> SECAUCUS --R1--> NY_PENN     (rail)
    MAYWOOD   --F1--> GLEN_ROCK                                      (feeder bus)
    FORT_LEE  --B1--> PABT                                           (express bus)
    HOB_PATH  --P1--> PATH_33                                        (PATH)
    HOBOKEN  ..walk.. HOB_PATH  (~25 m apart)
"""

from __future__ import annotations

import os
import zipfile

STOPS = {
    # stop_id: (name, lat, lon)
    "RIDGEWOOD": ("Ridgewood Station", 40.9793, -74.1163),
    "GLEN_ROCK": ("Glen Rock Main St", 40.9629, -74.1279),
    "SECAUCUS": ("Secaucus Junction", 40.7616, -74.0757),
    "NY_PENN": ("New York Penn Station", 40.7506, -73.9935),
    "MAYWOOD": ("Maywood Ave & Main", 40.9026, -74.0618),
    "FORT_LEE": ("Fort Lee Main St", 40.8509, -73.9701),
    "PABT": ("Port Authority Bus Terminal", 40.7570, -73.9903),
    "HOBOKEN": ("Hoboken Terminal", 40.7348, -74.0277),
    "HOB_PATH": ("Hoboken PATH", 40.7350, -74.0278),
    "PATH_33": ("33rd Street PATH", 40.7490, -73.9880),
    "DEADEND": ("Pine Barrens Park & Ride", 40.5000, -74.5000),
}

ROUTES = {
    # route_id: (short_name, long_name, route_type)
    "R1": ("BC", "Bergen County Line", 2),
    "F1": ("175", "Maywood Feeder", 3),
    "B1": ("159X", "Fort Lee Express", 3),
    "P1": ("PATH", "Hoboken - 33rd Street", 1),
    # Gives HOBOKEN real service that is useless for reaching Midtown, so the
    # only way out is the walk to the PATH platform. Without this, HOBOKEN
    # would have no trips at all and never enter the network.
    "L1": ("87", "Hoboken Local", 3),
}


def _hhmmss(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _mins(m: int) -> int:
    return m * 60


# (route, stop_id, offset-from-trip-start in minutes)
PATTERNS = {
    "R1": [("RIDGEWOOD", 0), ("GLEN_ROCK", 5), ("SECAUCUS", 30), ("NY_PENN", 45)],
    "F1": [("MAYWOOD", 0), ("GLEN_ROCK", 15)],
    "B1": [("FORT_LEE", 0), ("PABT", 35)],
    "P1": [("HOB_PATH", 0), ("PATH_33", 12)],
    "L1": [("HOBOKEN", 0), ("DEADEND", 40)],
}

# First departure and headway, in minutes past midnight / minutes.
SCHEDULES = {
    "R1": (6 * 60, 30, 5),   # 06:00, every 30 min, 5 trips -> last 08:00
    "F1": (6 * 60 + 10, 30, 4),  # 06:10 .. 07:40
    "B1": (6 * 60, 20, 9),   # 06:00 .. 08:40
    "P1": (6 * 60, 10, 20),  # 06:00 .. 09:10
    "L1": (6 * 60, 60, 3),   # 06:00 .. 08:00
}


def write_feed(path: str) -> str:
    """Write the fixture GTFS zip to ``path`` and return the path."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    stops = ["stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station"]
    for sid, (name, lat, lon) in STOPS.items():
        stops.append(f"{sid},{name},{lat},{lon},0,")

    routes = ["route_id,route_short_name,route_long_name,route_type"]
    for rid, (short, long, rtype) in ROUTES.items():
        routes.append(f"{rid},{short},{long},{rtype}")

    trips = ["route_id,service_id,trip_id,trip_headsign"]
    stop_times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]

    for rid, (start, headway, count) in SCHEDULES.items():
        for n in range(count):
            trip_id = f"{rid}_{n}"
            trips.append(f"{rid},WEEKDAY,{trip_id},inbound")
            base = _mins(start + n * headway)
            for seq, (sid, offset) in enumerate(PATTERNS[rid]):
                t = _hhmmss(base + _mins(offset))
                stop_times.append(f"{trip_id},{t},{t},{sid},{seq}")

    calendar = [
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
        "WEEKDAY,1,1,1,1,1,0,0,20260101,20261231",
    ]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("stops.txt", "\n".join(stops) + "\n")
        zf.writestr("routes.txt", "\n".join(routes) + "\n")
        zf.writestr("trips.txt", "\n".join(trips) + "\n")
        zf.writestr("stop_times.txt", "\n".join(stop_times) + "\n")
        zf.writestr("calendar.txt", "\n".join(calendar) + "\n")

    return path
