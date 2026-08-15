"""Geometry helpers: distances, and point-in-polygon ZIP lookup.

Everything here is dependency-free on purpose. The ZIP index is built from a
GeoJSON FeatureCollection of ZCTA polygons (see ``data/nj_zips.geojson``), and
is queried a few thousand times per pipeline run -- once per transit stop --
so it is backed by a uniform grid index rather than a linear scan.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Iterable, Iterator, Sequence

EARTH_RADIUS_M = 6_371_008.8

# A degree of latitude is ~111.32 km everywhere. Longitude shrinks with
# latitude; at NJ/NYC latitudes (~40.7 deg) the factor is ~0.758.
_M_PER_DEG_LAT = 111_320.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def m_per_deg_lon(lat: float) -> float:
    """Metres per degree of longitude at a given latitude."""
    return _M_PER_DEG_LAT * math.cos(math.radians(lat))


def deg_box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Bounding box (min_lat, min_lon, max_lat, max_lon) around a point.

    Used to pre-filter candidates before paying for a haversine call.
    """
    dlat = radius_m / _M_PER_DEG_LAT
    # Guard against the degenerate cos()->0 case near the poles.
    dlon = radius_m / max(m_per_deg_lon(lat), 1.0)
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _ring_contains(ring: Sequence[Sequence[float]], lon: float, lat: float) -> bool:
    """Ray-casting point-in-ring test. Ring coords are GeoJSON [lon, lat]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        # Does the edge straddle the horizontal ray at `lat`?
        if (yi > lat) != (yj > lat):
            # x coordinate where edge crosses the ray.
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _polygon_contains(polygon: Sequence[Sequence[Sequence[float]]], lon: float, lat: float) -> bool:
    """GeoJSON Polygon test: inside the outer ring and outside every hole."""
    if not polygon or not _ring_contains(polygon[0], lon, lat):
        return False
    return not any(_ring_contains(hole, lon, lat) for hole in polygon[1:])


def _iter_polygons(geometry: dict) -> Iterator[Sequence[Sequence[Sequence[float]]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        yield coords
    elif gtype == "MultiPolygon":
        yield from coords


def _bounds(polygon: Sequence[Sequence[Sequence[float]]]) -> tuple[float, float, float, float]:
    outer = polygon[0]
    lons = [p[0] for p in outer]
    lats = [p[1] for p in outer]
    return min(lons), min(lats), max(lons), max(lats)


class ZipIndex:
    """Grid-indexed point-in-polygon lookup from (lat, lon) to ZIP code.

    ZCTA polygons tile the state without overlapping, so the first polygon
    that contains the point is the answer.
    """

    #: Grid cell size in degrees. ~5.5km; NJ spans ~2.5x1.7 deg so this gives
    #: a few hundred cells, each holding a handful of candidate polygons.
    CELL_DEG = 0.05

    def __init__(self, features: Iterable[dict], zip_property: str = "ZCTA5CE10"):
        self._polys: list[tuple[str, Sequence, tuple[float, float, float, float]]] = []
        self._grid: dict[tuple[int, int], list[int]] = defaultdict(list)

        for feat in features:
            props = feat.get("properties") or {}
            zip_code = props.get(zip_property)
            if not zip_code:
                continue
            zip_code = str(zip_code).zfill(5)
            for poly in _iter_polygons(feat.get("geometry") or {}):
                if not poly:
                    continue
                bbox = _bounds(poly)
                idx = len(self._polys)
                self._polys.append((zip_code, poly, bbox))
                for cell in self._cells_for_bbox(bbox):
                    self._grid[cell].append(idx)

    @classmethod
    def from_geojson(cls, path: str, zip_property: str = "ZCTA5CE10") -> "ZipIndex":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(data.get("features") or [], zip_property=zip_property)

    def _cell(self, lon: float, lat: float) -> tuple[int, int]:
        return (int(math.floor(lon / self.CELL_DEG)), int(math.floor(lat / self.CELL_DEG)))

    def _cells_for_bbox(self, bbox: tuple[float, float, float, float]) -> Iterator[tuple[int, int]]:
        min_lon, min_lat, max_lon, max_lat = bbox
        x0, y0 = self._cell(min_lon, min_lat)
        x1, y1 = self._cell(max_lon, max_lat)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield (x, y)

    def lookup(self, lat: float, lon: float) -> str | None:
        """Return the ZIP containing the point, or None if outside coverage."""
        for idx in self._grid.get(self._cell(lon, lat), ()):
            zip_code, poly, (min_lon, min_lat, max_lon, max_lat) = self._polys[idx]
            if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                continue
            if _polygon_contains(poly, lon, lat):
                return zip_code
        return None

    @property
    def zip_codes(self) -> set[str]:
        return {z for z, _, _ in self._polys}

    def __len__(self) -> int:
        return len(self._polys)
