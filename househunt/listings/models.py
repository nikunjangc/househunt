"""House / Listing / ListCache.

Ported from the original Python 2 ``househunt.househunt``. The data model and
the hash-based cache identity are the parts worth keeping; the ~40 hand-written
property pairs that only did type coercion are replaced by dataclasses plus the
``_int``/``_float`` helpers, and the dead Zillow (ZWSID, retired) and Redfin
scraping clients are gone.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timedelta
from typing import Any


def _int(value: Any) -> int | None:
    """Best-effort int coercion; None when the value is missing or junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            # Handles "3.0" and "1,250" style values from CSV/HTML sources.
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            return None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class House:
    """Physical description of a property."""

    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    beds: int | None = None
    baths: float | None = None
    sq_ft: int | None = None
    parking: int | None = None
    parking_type: str | None = None
    lot_size: int | None = None
    home_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    def __post_init__(self) -> None:
        self.street_address = _str(self.street_address)
        self.city = _str(self.city)
        self.state = _str(self.state)
        # ZIPs are identifiers, not numbers -- keep leading zeros, which
        # matters for every NJ ZIP (they all start with 0).
        self.zip_code = _str(self.zip_code)
        if self.zip_code and self.zip_code.isdigit():
            self.zip_code = self.zip_code.zfill(5)
        self.beds = _int(self.beds)
        self.baths = _float(self.baths)
        self.sq_ft = _int(self.sq_ft)
        self.parking = _int(self.parking)
        self.parking_type = _str(self.parking_type)
        self.lot_size = _int(self.lot_size)
        self.home_type = _str(self.home_type)
        self.latitude = _float(self.latitude)
        self.longitude = _float(self.longitude)

    def __str__(self) -> str:
        return f"{self.street_address} {self.city}, {self.state} {self.zip_code}"

    @property
    def hsh(self) -> str:
        """Stable identity for caching: md5 of the normalized address."""
        key = "|".join(
            (self.street_address or "", self.city or "", self.state or "", self.zip_code or "")
        ).lower()
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    @property
    def detailed(self) -> str:
        return (
            f"Address: {self}\n"
            f"Home Type: {self.home_type}\n"
            f"Beds: {self.beds}\n"
            f"Baths: {self.baths}\n"
            f"SqFt: {self.sq_ft}\n"
            f"Lot Size: {self.lot_size}\n"
            f"Parking Spaces: {self.parking}\n"
            f"Parking Type: {self.parking_type}\n"
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, dictionary: dict[str, Any]) -> "House":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dictionary.items() if k in known})

    def matches_search(
        self,
        beds: int | None = None,
        baths: float | None = None,
        sq_ft: int | None = None,
        home_types: list[str] | None = None,
    ) -> bool:
        """True when the house meets every supplied minimum.

        A missing value on the listing fails a criterion that was asked for --
        an unknown bedroom count is not evidence of enough bedrooms.
        """
        if beds is not None and (self.beds is None or self.beds < beds):
            return False
        if baths is not None and (self.baths is None or self.baths < baths):
            return False
        if sq_ft is not None and (self.sq_ft is None or self.sq_ft < sq_ft):
            return False
        if home_types:
            wanted = {t.lower() for t in home_types}
            if self.home_type is None or self.home_type.lower() not in wanted:
                return False
        return True


@dataclass
class Listing:
    """A house on the market, plus the commute facts we score it by."""

    house: House = field(default_factory=House)
    list_price: int | None = None
    days_on_market: int | None = None
    original_list_price: int | None = None
    status: str | None = None
    mls_id: str | None = None
    open_house_date: str | None = None
    open_house_start_time: str | None = None
    open_house_end_time: str | None = None
    url: str | None = None
    source: str | None = None
    #: Minutes to Midtown Manhattan, joined in from the commute pipeline.
    commute_minutes: int | None = None
    commute_transfers: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.house, dict):
            self.house = House.from_dict(self.house)
        self.list_price = _int(self.list_price)
        self.days_on_market = _int(self.days_on_market)
        self.original_list_price = _int(self.original_list_price)
        self.status = _str(self.status)
        self.mls_id = _str(self.mls_id)
        self.commute_minutes = _int(self.commute_minutes)
        self.commute_transfers = _int(self.commute_transfers)

    def __str__(self) -> str:
        price = f"${self.list_price:,}" if self.list_price else "price n/a"
        return f"{self.house} - {price}"

    @property
    def hsh(self) -> str:
        return self.house.hsh

    @property
    def detailed(self) -> str:
        return (
            f"House Details:\n{self.house.detailed}\n"
            f"Status: {self.status}\n"
            f"List Price: {self.list_price}\n"
            f"MLS ID: {self.mls_id}\n"
            f"Days on Market: {self.days_on_market}\n"
            f"Original Price: {self.original_list_price}\n"
            f"Commute: {self.commute_minutes} min, {self.commute_transfers} transfers\n"
        )

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["house"] = self.house.as_dict()
        return data

    @classmethod
    def from_dict(cls, dictionary: dict[str, Any]) -> "Listing":
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in dictionary.items() if k in known}
        if isinstance(payload.get("house"), dict):
            payload["house"] = House.from_dict(payload["house"])
        return cls(**payload)

    def matches_search(
        self,
        min_price: int | None = None,
        max_price: int | None = None,
        max_commute_minutes: int | None = None,
        max_transfers: int | None = None,
    ) -> bool:
        if min_price is not None and (self.list_price is None or self.list_price < min_price):
            return False
        if max_price is not None and (self.list_price is None or self.list_price > max_price):
            return False
        if max_commute_minutes is not None and (
            self.commute_minutes is None or self.commute_minutes > max_commute_minutes
        ):
            return False
        if max_transfers is not None and (
            self.commute_transfers is None or self.commute_transfers > max_transfers
        ):
            return False
        return True


class ListCache:
    """TinyDB-backed listing cache with a TTL, keyed by address hash.

    Same design as the original: it exists so repeated runs do not re-fetch
    (and, historically, so a rate-limited API quota was not burned).
    """

    DB_FILE = "listing_db.json"
    DB_TTL = timedelta(hours=12)

    def __init__(self, db_path: str | None = None, ttl: timedelta | None = None):
        from tinydb import TinyDB  # imported lazily so geo/commute code has no dep

        self.ttl = ttl or self.DB_TTL
        path = db_path or os.path.join(os.getcwd(), self.DB_FILE)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = TinyDB(path)

    def _query(self):
        from tinydb import Query

        return Query()

    def listing_in_cache(self, listing: Listing) -> bool:
        return self.db.contains(self._query().hsh == listing.hsh)

    def retrieve_listing(self, listing: Listing) -> Listing | None:
        row = self.db.get(self._query().hsh == listing.hsh)
        return Listing.from_dict(row) if row else None

    def insert_listing(self, listing: Listing) -> None:
        row = listing.as_dict()
        row["last_updated"] = datetime.now().isoformat()
        row["hsh"] = listing.hsh
        if self.listing_in_cache(listing):
            self.db.update(row, self._query().hsh == listing.hsh)
        else:
            self.db.insert(row)

    def remove_listing(self, listing: Listing) -> None:
        self.db.remove(self._query().hsh == listing.hsh)

    def remove_old_listings(self) -> int:
        """Drop entries past the TTL. Returns how many were removed."""
        cutoff = datetime.now() - self.ttl
        stale = []
        for row in self.db.all():
            stamp = row.get("last_updated")
            if not stamp:
                stale.append(row.get("hsh"))
                continue
            try:
                if datetime.fromisoformat(stamp) < cutoff:
                    stale.append(row.get("hsh"))
            except ValueError:
                stale.append(row.get("hsh"))
        if stale:
            self.db.remove(self._query().hsh.one_of(stale))
        return len(stale)
