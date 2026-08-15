"""Listing backends.

Two are provided, and which one you can use is a licensing question, not a
technical one:

:class:`ResoWebApiSource`
    The sanctioned route. NJMLS distributes listing data to licensed IDX
    participants as a feed (through RE/Advantage), and modern MLS feeds speak
    the RESO Web API -- OData over HTTPS with a bearer token. If you have (or
    your agent has) feed credentials, point this at the endpoint and it works
    with no scraping at all. Field names below are RESO Data Dictionary
    standard, so the same code works against most MLSs.

:class:`NjmlsPortalSource`
    Queries the public njmls.com search the way a browser does. Listing data
    on the portal is IDX-licensed and the site's terms govern automated
    access, so this class refuses to run until you pass
    ``acknowledge_terms=True``, and it checks robots.txt first. Treat it as a
    fallback for personal use, keep the rate limit low, and prefer the feed.

The response-parsing hook on the portal source is deliberately pluggable: the
live markup could not be inspected from the build environment (njmls.com is
blocked by the egress policy here), so ``parse_html`` must be fitted against
a real response before that backend returns anything useful.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

from .models import House, Listing
from .search import SearchCriteria

DEFAULT_TIMEOUT = 30
USER_AGENT = "househunt/0.7 (personal home search; +https://github.com/nikunjangc/househunt)"


# ---------------------------------------------------------------------------
# RESO Web API (licensed feed)
# ---------------------------------------------------------------------------


def _odata_quote(value: str) -> str:
    """Escape a string literal for an OData $filter."""
    return "'" + str(value).replace("'", "''") + "'"


class ResoWebApiSource:
    """RESO Web API / OData backend.

    ``base_url`` is the service root, e.g.
    ``https://api.example-mls.com/reso/odata``. Listings are fetched from the
    ``Property`` resource with server-side paging.
    """

    name = "reso"

    #: RESO Data Dictionary field -> our model attribute.
    FIELD_MAP = {
        "UnparsedAddress": "street_address",
        "City": "city",
        "StateOrProvince": "state",
        "PostalCode": "zip_code",
        "BedroomsTotal": "beds",
        "BathroomsTotalInteger": "baths",
        "LivingArea": "sq_ft",
        "LotSizeSquareFeet": "lot_size",
        "PropertySubType": "home_type",
        "GarageSpaces": "parking",
        "Latitude": "latitude",
        "Longitude": "longitude",
    }

    SELECT_FIELDS = tuple(FIELD_MAP) + (
        "ListingId",
        "ListPrice",
        "OriginalListPrice",
        "DaysOnMarket",
        "StandardStatus",
        "ListingKey",
    )

    def __init__(
        self,
        base_url: str,
        access_token: str,
        resource: str = "Property",
        page_size: int = 200,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.resource = resource
        self.page_size = page_size
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    def build_filter(self, zip_code: str, criteria: SearchCriteria) -> str:
        clauses = [f"PostalCode eq {_odata_quote(zip_code)}"]
        if criteria.statuses:
            status = " or ".join(
                f"StandardStatus eq {_odata_quote(s)}" for s in criteria.statuses
            )
            clauses.append(f"({status})")
        if criteria.property_types:
            ptype = " or ".join(
                f"PropertyType eq {_odata_quote(p)}" for p in criteria.property_types
            )
            clauses.append(f"({ptype})")
        if criteria.min_price is not None:
            clauses.append(f"ListPrice ge {int(criteria.min_price)}")
        if criteria.max_price is not None:
            clauses.append(f"ListPrice le {int(criteria.max_price)}")
        if criteria.min_beds is not None:
            clauses.append(f"BedroomsTotal ge {int(criteria.min_beds)}")
        if criteria.min_baths is not None:
            clauses.append(f"BathroomsTotalInteger ge {int(criteria.min_baths)}")
        if criteria.min_sq_ft is not None:
            clauses.append(f"LivingArea ge {int(criteria.min_sq_ft)}")
        return " and ".join(clauses)

    def search_zip(self, zip_code: str, criteria: SearchCriteria) -> list[Listing]:
        url = f"{self.base_url}/{self.resource}"
        params = {
            "$filter": self.build_filter(zip_code, criteria),
            "$select": ",".join(self.SELECT_FIELDS),
            "$top": str(min(self.page_size, criteria.limit_per_zip)),
            "$orderby": "ListPrice asc",
        }

        listings: list[Listing] = []
        while url and len(listings) < criteria.limit_per_zip:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            for row in payload.get("value", []):
                listings.append(self.to_listing(row))
                if len(listings) >= criteria.limit_per_zip:
                    break
            # @odata.nextLink carries its own querystring.
            url = payload.get("@odata.nextLink") or ""
            params = {}
        return listings

    def to_listing(self, row: dict[str, Any]) -> Listing:
        house_kwargs = {
            attr: row.get(field) for field, attr in self.FIELD_MAP.items()
        }
        return Listing(
            house=House(**house_kwargs),
            list_price=row.get("ListPrice"),
            original_list_price=row.get("OriginalListPrice"),
            days_on_market=row.get("DaysOnMarket"),
            status=row.get("StandardStatus"),
            mls_id=row.get("ListingId") or row.get("ListingKey"),
            source=self.name,
        )


# ---------------------------------------------------------------------------
# NJMLS public portal
# ---------------------------------------------------------------------------


class TermsNotAcknowledged(RuntimeError):
    pass


class RobotsDisallowed(RuntimeError):
    pass


@dataclass
class NjmlsEndpoint:
    """Where the portal's search lives.

    Split out so it can be corrected without touching the client: portals move
    their search endpoint and rename form fields regularly.
    """

    home_url: str = "https://www.njmls.com/"
    search_url: str = "https://www.njmls.com/listings/index.cfm"
    #: Form fields the search POST expects. Verify against a real request
    #: (browser devtools -> Network -> the search POST) before relying on it.
    field_names: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.field_names is None:
            self.field_names = {
                "zip": "zipcode",
                "min_price": "pricemin",
                "max_price": "pricemax",
                "min_beds": "beds",
                "min_baths": "baths",
                "page": "page",
            }


class NjmlsPortalSource:
    """Session-based NJMLS portal search.

    Establishes a session once (picking up cookies from the landing page) and
    reuses it for every ZIP query, which is what the portal expects and is far
    lighter on it than a cold connection per request.
    """

    name = "njmls"

    def __init__(
        self,
        endpoint: NjmlsEndpoint | None = None,
        acknowledge_terms: bool = False,
        respect_robots: bool = True,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        parse_html: Callable[[str], list[dict[str, Any]]] | None = None,
    ):
        if not acknowledge_terms:
            raise TermsNotAcknowledged(
                "NJMLS listing data is IDX-licensed and its use is governed by "
                "njmls.com's terms. Review them, and prefer a licensed IDX / "
                "RESO feed (ResoWebApiSource) where you can get one. Pass "
                "acknowledge_terms=True once you have confirmed your use is "
                "permitted."
            )
        self.endpoint = endpoint or NjmlsEndpoint()
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._parse_html = parse_html or parse_njmls_html
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._established = False

    # -- session ---------------------------------------------------------

    def check_robots(self) -> None:
        parsed = urllib.parse.urlparse(self.endpoint.home_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
        except Exception as exc:  # noqa: BLE001
            raise RobotsDisallowed(
                f"could not read {robots_url} ({exc}); refusing to guess"
            ) from exc
        if not parser.can_fetch(USER_AGENT, self.endpoint.search_url):
            raise RobotsDisallowed(
                f"robots.txt at {robots_url} disallows {self.endpoint.search_url} "
                f"for this user agent"
            )

    def establish_session(self) -> None:
        """Warm the session: robots check, then collect cookies."""
        if self._established:
            return
        if self.respect_robots:
            self.check_robots()
        response = self.session.get(self.endpoint.home_url, timeout=self.timeout)
        response.raise_for_status()
        self._established = True

    # -- search ----------------------------------------------------------

    def build_params(self, zip_code: str, criteria: SearchCriteria, page: int = 1) -> dict:
        f = self.endpoint.field_names
        params = {f["zip"]: zip_code, f["page"]: str(page)}
        if criteria.min_price is not None:
            params[f["min_price"]] = str(int(criteria.min_price))
        if criteria.max_price is not None:
            params[f["max_price"]] = str(int(criteria.max_price))
        if criteria.min_beds is not None:
            params[f["min_beds"]] = str(int(criteria.min_beds))
        if criteria.min_baths is not None:
            params[f["min_baths"]] = str(int(criteria.min_baths))
        return params

    def search_zip(self, zip_code: str, criteria: SearchCriteria) -> list[Listing]:
        self.establish_session()
        listings: list[Listing] = []
        page = 1
        while len(listings) < criteria.limit_per_zip:
            response = self.session.get(
                self.endpoint.search_url,
                params=self.build_params(zip_code, criteria, page),
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = self._parse_html(response.text)
            if not rows:
                break
            for row in rows:
                listings.append(self.to_listing(row, zip_code))
                if len(listings) >= criteria.limit_per_zip:
                    break
            page += 1
            if page > 50:  # hard stop against a pagination loop
                break
        return listings

    def to_listing(self, row: dict[str, Any], zip_code: str) -> Listing:
        return Listing(
            house=House(
                street_address=row.get("address"),
                city=row.get("city"),
                state=row.get("state") or "NJ",
                zip_code=row.get("zip") or zip_code,
                beds=row.get("beds"),
                baths=row.get("baths"),
                sq_ft=row.get("sq_ft"),
                home_type=row.get("home_type"),
            ),
            list_price=row.get("price"),
            days_on_market=row.get("days_on_market"),
            status=row.get("status") or "Active",
            mls_id=row.get("mls_id"),
            url=row.get("url"),
            source=self.name,
        )


#: Embedded-JSON blobs are how most listing portals hydrate their result grid;
#: finding one is far more stable than matching on CSS classes.
_JSON_BLOB = re.compile(
    r"<script[^>]+type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


def parse_njmls_html(html: str) -> list[dict[str, Any]]:
    """Extract listing rows from a portal search response.

    Tries embedded JSON-LD first, which portals publish for search engines and
    which is far more stable than scraping the rendered grid.

    This could not be validated against a live response from the build
    environment, so if it returns nothing, capture one real search response and
    fit a parser to it, then pass it in as ``parse_html=``.
    """
    rows: list[dict[str, Any]] = []
    for match in _JSON_BLOB.finditer(html):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        rows.extend(_walk_jsonld(payload))
    return rows


def _walk_jsonld(node: Any) -> Iterable[dict[str, Any]]:
    """Yield normalized listing dicts from schema.org objects."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("@type") or ""
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(t in ("SingleFamilyResidence", "Residence", "House", "Apartment", "Product")
           for t in types):
        address = node.get("address") or {}
        if isinstance(address, dict):
            offers = node.get("offers") or {}
            price = offers.get("price") if isinstance(offers, dict) else None
            yield {
                "address": address.get("streetAddress"),
                "city": address.get("addressLocality"),
                "state": address.get("addressRegion"),
                "zip": address.get("postalCode"),
                "beds": node.get("numberOfRooms") or node.get("numberOfBedrooms"),
                "baths": node.get("numberOfBathroomsTotal"),
                "sq_ft": (node.get("floorSize") or {}).get("value")
                if isinstance(node.get("floorSize"), dict)
                else None,
                "price": price or node.get("price"),
                "url": node.get("url"),
                "mls_id": node.get("sku") or node.get("identifier"),
                "home_type": types[0] if types else None,
            }

    for key in ("itemListElement", "mainEntity", "about", "item", "@graph"):
        if key in node:
            yield from _walk_jsonld(node[key])
