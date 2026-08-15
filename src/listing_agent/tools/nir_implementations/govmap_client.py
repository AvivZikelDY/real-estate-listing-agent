"""Live async client for the Israeli government's govmap real-estate API.

WHAT THIS IS
------------
This is the real data source behind `price_signal` — the public record of
Israeli property transactions. It is the same API that any "nadlan MCP"
server wraps. We call the HTTP endpoints directly rather than going
through MCP, because MCP exists so that an *LLM* can discover tools at
runtime; our caller is deterministic Python that already knows exactly
which endpoint it wants, so the protocol layer would add a handshake and a
tool-listing round trip for no benefit. Tools here are deterministic
data/logic only (`docs/nir_handoff.md:224`).

No authentication, no API key. Base URL `https://www.govmap.gov.il/api`.

THE FOUR-STEP DANCE
-------------------
There is no "give me deals near this address" endpoint. Getting from an
address to transactions takes four hops:

    1. POST /search-service/autocomplete      address  -> WKT point
    2. GET  /real-estate/deals/{x},{y}/{r}    point    -> polygon ids   (!)
    3. GET  /real-estate/street-deals/{id}    polygon  -> transactions
    4. GET  /real-estate/neighborhood-deals/{id}       -> transactions

(!) Step 2 is the trap: despite the `/deals/` path it returns no
transactions at all, only polygon metadata. See `polygons_by_radius`.

>>> THIS IS THE LIVE PATH, WITH NO FALLBACK <<<
------------------------------------------------
`NirPriceSignal.execute()` calls this on every request. There is no
offline snapshot fallback: a failure here propagates to `status="error"`.

MEASURED LATENCY: a full `find_comparable_deals()` for a Tel Aviv address
takes **2.6-2.9 seconds** end to end (~450 deals returned). The earlier
assumption that ~21 throttled calls could not finish in time was wrong:
deduplicating polygons and dropping empty ones cuts the count well below
the worst case, and steps 3 and 4 run concurrently per polygon.

`comparables_snapshot.json` and `build_snapshot.py` still exist as an
offline extract (useful for tests, or as a reference point), but nothing
in the live request path reads them anymore.
"""

import asyncio
import logging

import httpx
from pydantic import BaseModel, Field

from src.listing_agent.tools.nir_implementations.coordinates import parse_wkt_point

# NOTE: the typed tool errors (`ToolTimeoutError` and friends) are imported
# lazily inside `_request`, NOT here at module level. They live in
# `src.listing_agent.agent.tool_executor.errors`, and importing anything
# from that package runs its `__init__.py`, which imports
# `tools.dependency`, which imports this very package — a circular import
# that fails at startup. Deferring the import to call time breaks the cycle
# while keeping the error taxonomy that `docs/nir_handoff.md:73-86`
# explicitly invites tools to use.

logger = logging.getLogger(__name__)

# --- Endpoint configuration ------------------------------------------------

GOVMAP_BASE_URL = "https://www.govmap.gov.il/api"

# govmap's deal-type filter. 1 = first-hand (new, straight from the
# developer), 2 = second-hand (resale). We want 2: resales are what an
# ordinary purchase listing should be compared against, because new-build
# prices carry a developer premium that would drag the median upward.
DEAL_TYPE_SECOND_HAND = 2

# Radius ladder for finding the address's OWN land parcel.
#
# We want the smallest radius that finds anything, because the closest
# parcel to the address point is the address's own building — and that is
# what makes the whole lookup correct by construction (see
# `find_comparable_deals`). Each rung is tried in turn and the first one to
# return a polygon wins.
#
# NOTE these are Web Mercator units, not true ground metres — inflated by
# ~1.18x at Tel Aviv's latitude (see `coordinates.py`), so 10 here is about
# 8.5 real metres.
#
# The ladder exists because a single small radius is not reliable: measured
# across 10 Tel Aviv addresses, radius 10 found a parcel for 9 of them but
# returned NOTHING for "דיזנגוף 200", which would have reported a perfectly
# valid address as having no comparable data. It needs 25.
RADIUS_LADDER: tuple[int, ...] = (5, 10, 25, 100)

# Kept for callers that want the raw polygon lookup at a chosen radius.
DEFAULT_RADIUS = 100

# Cap on polygons returned by `polygons_by_radius`. `find_comparable_deals`
# only ever uses the first one, but the cap keeps the helper honest for any
# other caller.
MAX_POLYGONS = 10

# Deals requested per polygon endpoint. The API caps a page at 500 and
# reports the true size in `totalCount`; 500 recent deals per polygon is
# far more than any median needs, so we never paginate.
DEALS_PER_POLYGON = 500

# Per-request timeout. Deliberately well under the 8s executor ceiling so
# a slow upstream surfaces as our own typed `ToolTimeoutError` — which the
# executor retries once — rather than as the executor's blunt kill.
REQUEST_TIMEOUT_S = 5.0

# Minimum gap between requests (~5 req/s), matching the rate the public
# reference client uses against this API.
_MIN_REQUEST_INTERVAL_S = 0.2

# --- Residential filtering -------------------------------------------------
#
# >>> THIS FILTER IS NOT OPTIONAL. <<<
#
# The deal endpoints return *every* registered transaction in the polygon,
# not just apartments. A real sample of 500 deals around Rothschild St came
# back as: 406 apartments, 29 offices, 20 shops, 15 parking spaces, 14
# storage units, 8 land plots.
#
# Mixing those into a residential price-per-sqm median is not a small
# inaccuracy, it is garbage: one office in that sample was 500 sqm at
# 33,337,000 ILS (~67k/sqm), and a parking space is a handful of sqm at a
# price that implies a wild per-sqm figure. Note that a sanity band on
# price-per-sqm does NOT catch these — a parking space can land squarely
# inside a plausible per-sqm range. Only the deal's declared nature does.
#
# `dealNatureDescription` is the field that carries this. Whitelisted
# rather than blacklisted, so an unfamiliar new category is excluded by
# default instead of silently polluting the median.
_RESIDENTIAL_DEAL_NATURES = frozenset(
    {
        "דירה",  # apartment
        "דירה בבית קומות",  # apartment in a multi-storey building (the common case)
        "דירת גן",  # garden apartment
        "דירת גג",  # roof apartment
        "פנטהאוז",  # penthouse
        "מיני פנטהאוז",  # mini penthouse
        "דופלקס",  # duplex
        "קוטג'",  # cottage / townhouse
        "קוטג",  # same, seen without the apostrophe
        "בית בודד",  # detached house
        "דו משפחתי",  # semi-detached
        "בית דו משפחתי",  # semi-detached, longer form
        "וילה",  # villa
        "מגורים",  # generic "residential"
    }
)


class ComparableDeal(BaseModel):
    """One historical transaction, normalised into our own vocabulary.

    IMPORTANT: this is the shared currency between the live path and the
    offline snapshot. Both produce a list of exactly this type, so the
    price maths in `price_signal.py` is identical either way and switching
    between them cannot silently change a number.

    govmap's raw JSON uses its own names (`dealAmount`, `assetArea`,
    `assetRoomNum`, `streetNameHeb`); those are deliberately not leaked
    past this module.
    """

    price: float = Field(gt=0, description="Transaction amount in ILS.")
    area_sqm: float = Field(gt=0, description="Registered floor area in square metres.")
    rooms: float | None = Field(default=None, description="Room count, when reported.")
    deal_date: str | None = Field(default=None, description="Transaction date, ISO-8601.")
    street: str | None = None
    neighborhood: str | None = None

    @property
    def price_per_sqm(self) -> float:
        """The single figure the whole price comparison is built on."""
        return self.price / self.area_sqm


class GovmapClient:
    """Async httpx client wrapping the four govmap endpoints we need.

    Use as an async context manager so the connection pool is always closed:

        async with GovmapClient() as client:
            deals = await client.find_comparable_deals("רוטשילד 30 תל אביב")
    """

    def __init__(
        self,
        base_url: str = GOVMAP_BASE_URL,
        timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # Created lazily in __aenter__ so constructing the client is cheap
        # and side-effect free. This matters: `price_signal` constructs one
        # even on the offline path, and must not open sockets to do so.
        self._http: httpx.AsyncClient | None = None
        # Timestamp of the last request, for the self-imposed rate limit.
        self._last_request_at = 0.0

    async def __aenter__(self) -> "GovmapClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout_s,
            headers={
                # A browser-ish UA: the API is unfriendly to default
                # library user agents.
                "User-Agent": "Mozilla/5.0 (compatible; listing-agent/0.1)",
            },
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- internal plumbing -------------------------------------------------

    async def _throttle(self) -> None:
        """Sleep just enough to stay under the self-imposed request rate.

        Called before every HTTP request. Without it, querying 10 polygons
        back to back would burst ~20 requests instantly and invite a 429
        from a free public service.
        """
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request_at
        if elapsed < _MIN_REQUEST_INTERVAL_S:
            await asyncio.sleep(_MIN_REQUEST_INTERVAL_S - elapsed)
        self._last_request_at = loop.time()

    async def _request(self, method: str, url: str, **kwargs) -> object:
        """Perform one HTTP request and return parsed JSON.

        This is where every transport failure becomes one of the project's
        *typed* tool errors, and that translation is the whole point:
        `LocalToolExecutor` reads the error code and automatically retries
        once for timeout / rate-limited / provider-unavailable, but never
        for anything else (`local.py:41-49`, `RETRYABLE_TOOL_ERROR_CODES`).
        Returning a plain `ToolResult(status="error")` instead would flatten
        all of these into a non-retryable `internal_error`.
        """
        # Deliberately a local import — see the note at the top of this
        # module about the circular import this avoids.
        from src.listing_agent.agent.tool_executor.errors import (
            ToolProviderUnavailableError,
            ToolRateLimitedError,
            ToolTimeoutError,
        )

        if self._http is None:
            raise RuntimeError(
                "GovmapClient must be used as an async context manager "
                "(`async with GovmapClient() as client:`)."
            )
        await self._throttle()
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            # Upstream took too long. Transient, so worth one retry.
            raise ToolTimeoutError(f"govmap request timed out after {self._timeout_s}s.") from exc
        except httpx.HTTPError as exc:
            # DNS failure, connection refused, TLS problem, and similar.
            raise ToolProviderUnavailableError("govmap is unreachable.") from exc

        # 429: we are being throttled, so a retry after a pause may work.
        if response.status_code == 429:
            raise ToolRateLimitedError("govmap rate-limited the request (HTTP 429).")
        # 5xx: upstream fault, also potentially transient.
        if response.status_code >= 500:
            raise ToolProviderUnavailableError(
                f"govmap returned a server error (HTTP {response.status_code})."
            )
        # Any other 4xx means we sent something wrong. Not retryable, so it
        # is left to surface as a generic internal error.
        response.raise_for_status()

        try:
            return response.json()
        except ValueError as exc:
            raise ToolProviderUnavailableError("govmap returned a non-JSON response.") from exc

    # --- step 1: address -> coordinates ------------------------------------

    async def autocomplete(self, search_text: str) -> list[dict]:
        """Resolve a free-text address to candidate locations.

        POST /search-service/autocomplete

        This is also precisely the endpoint a real `geo_resolver` would
        need: the result list yields a canonical `text` (-> normalized_address),
        multiple entries (-> the `ambiguous` outcome and its `candidates`),
        and a coordinate (-> lat/lon once converted from Web Mercator).

        Returns:
            The raw `results` list. Each entry carries `text`, `id`, `type`,
            `score`, and `shape` (a WKT `POINT(x y)` in Web Mercator metres).
            Returns `[]` when nothing matches — a business "no answer", not
            an error.
        """
        payload = {
            "searchText": search_text,
            # The dataset is Hebrew. Querying in English returns far fewer
            # hits, and misses most street names entirely.
            "language": "he",
            # Fuzzy matching, because triage_planner extracts addresses
            # from informally written listings.
            "isAccurate": False,
            "maxResults": 10,
        }
        data = await self._request(
            "POST", f"{self._base_url}/search-service/autocomplete", json=payload
        )
        if not isinstance(data, dict):
            return []
        results = data.get("results", [])
        return results if isinstance(results, list) else []

    # --- step 2: coordinates -> polygon ids --------------------------------

    async def polygons_by_radius(
        self, x: float, y: float, radius: int = DEFAULT_RADIUS
    ) -> list[str]:
        """Find deal-polygon IDs near a projected point.

        GET /real-estate/deals/{x},{y}/{radius}

        >>> GOTCHA <<< Despite the `/deals/` path, this endpoint returns NO
        transactions. It returns only polygon metadata — `polygon_id`,
        `dealscount`, `settlementNameHeb`, `streetNameHeb`, `houseNum`,
        `objectid`. Those polygon IDs then feed the two endpoints below,
        which are what actually return deals. This is the most confusing
        part of the API and the thing most likely to mislead a reader.

        Coordinates go in the URL *path*, comma-separated, in Web Mercator
        metres — not as lat/lon query parameters.
        """
        url = f"{self._base_url}/real-estate/deals/{x},{y}/{radius}"
        data = await self._request("GET", url)
        if not isinstance(data, list):
            return []

        polygon_ids: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            polygon_id = entry.get("polygon_id")
            if not polygon_id:
                continue
            # CAREFUL: `dealscount` arrives as a *string* ("2", "0"), so a
            # plain truthiness test would treat "0" as true and waste two
            # HTTP calls on a polygon with nothing in it.
            try:
                deals_count = int(entry.get("dealscount") or 0)
            except (TypeError, ValueError):
                deals_count = 0
            if deals_count > 0:
                polygon_ids.append(str(polygon_id))

        # Deduplicate while preserving the API's own ordering (roughly
        # nearest first), then cap to bound total latency.
        seen: set[str] = set()
        unique = [p for p in polygon_ids if not (p in seen or seen.add(p))]
        return unique[:MAX_POLYGONS]

    # --- steps 3 & 4: polygon id -> actual deals ---------------------------

    async def street_deals(self, polygon_id: str) -> list[ComparableDeal]:
        """Transactions on the same street as `polygon_id`.

        GET /real-estate/street-deals/{polygon_id}

        The tighter of the two nets, and the better comparable: same street
        means genuinely similar location value.
        """
        return await self._fetch_deals(f"{self._base_url}/real-estate/street-deals/{polygon_id}")

    async def neighborhood_deals(self, polygon_id: str) -> list[ComparableDeal]:
        """Transactions across the wider neighbourhood around `polygon_id`.

        GET /real-estate/neighborhood-deals/{polygon_id}

        Same response envelope as `street_deals`, wider geographic net. It
        is what saves the comparison when a single street has too few sales
        to form a meaningful median.
        """
        return await self._fetch_deals(
            f"{self._base_url}/real-estate/neighborhood-deals/{polygon_id}"
        )

    async def _fetch_deals(self, url: str) -> list[ComparableDeal]:
        """Shared implementation behind the two deal endpoints.

        Both return the same `{data: [...], totalCount, limit, offset}`
        envelope, so parsing and filtering live in one place.
        """
        params = {"limit": DEALS_PER_POLYGON, "dealType": DEAL_TYPE_SECOND_HAND}
        data = await self._request("GET", url, params=params)

        # Tolerate both the documented envelope and a bare list.
        if isinstance(data, dict):
            rows = data.get("data", [])
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not isinstance(rows, list):
            return []

        deals: list[ComparableDeal] = []
        for row in rows:
            deal = self._parse_deal(row)
            # A single malformed or non-residential row must never abort
            # the lookup — skip it and keep going.
            if deal is not None:
                deals.append(deal)
        return deals

    @staticmethod
    def _parse_deal(row: object) -> ComparableDeal | None:
        """Map one raw govmap deal onto `ComparableDeal`, or reject it.

        Returns `None` — never raises — when the row is unusable:

          * not a residential transaction (see `_RESIDENTIAL_DEAL_NATURES`;
            this is the filter that keeps offices, shops, parking spaces
            and storage units out of a residential median);
          * missing `dealAmount` or `assetArea`, without which no
            price-per-sqm exists at all;
          * a non-positive price or area, which would fail the model's
            `gt=0` constraints.
        """
        if not isinstance(row, dict):
            return None

        # Reject non-residential first — it is the cheapest check and the
        # one that matters most for correctness.
        if row.get("dealNatureDescription") not in _RESIDENTIAL_DEAL_NATURES:
            return None

        try:
            price = float(row["dealAmount"])
            area = float(row["assetArea"])
        except (KeyError, TypeError, ValueError):
            return None
        # Guard the `gt=0` constraints before constructing the model, so a
        # zero/negative row is skipped rather than raising here.
        if price <= 0 or area <= 0:
            return None

        rooms_raw = row.get("assetRoomNum")
        try:
            rooms = float(rooms_raw) if rooms_raw is not None else None
        except (TypeError, ValueError):
            rooms = None

        return ComparableDeal(
            price=price,
            area_sqm=area,
            rooms=rooms,
            # ISO-8601 with a time component, e.g. "2026-06-02T00:00:00.000Z".
            deal_date=str(row["dealDate"]) if row.get("dealDate") else None,
            # Hebrew street name; the API also offers `streetNameEng`, but
            # Hebrew is the field that is reliably populated.
            street=row.get("streetNameHeb"),
            # Hebrew neighbourhood name, e.g. "לב העיר". This is why
            # `price_signal` needs Hebrew aliases in its lookup.
            neighborhood=row.get("neighborhood"),
        )

    # --- the full workflow -------------------------------------------------

    async def find_polygon_for_address(
        self, address: str
    ) -> tuple[str, int] | None:
        """Locate the land parcel the address itself sits on.

        This is the heart of the design. Instead of sweeping a wide radius
        and trying to clean up afterwards, we deliberately find the SMALLEST
        radius that returns anything — because the nearest parcel to the
        address point is the address's own building.

        Why that matters: every polygon belongs to exactly one
        neighbourhood, so the address's own polygon belongs to the address's
        own neighbourhood, by construction. There is nothing to filter, and
        no way for two neighbourhoods to get blended together.

        Contrast the previous wide-radius approach: a 300-unit search around
        "אילת 30" returned 10 polygons split 5/5 between צפון יפו and
        נווה צדק — two different markets averaged into one meaningless
        median. The single-polygon approach cannot express that bug.

        The ladder (`RADIUS_LADDER`) exists because one fixed small radius
        is not reliable. Measured over 10 Tel Aviv addresses, radius 10
        found a parcel for 9 of them but nothing at all for "דיזנגוף 200",
        which needs 25. Each extra rung is one cheap metadata call and only
        happens when the smaller ones came back empty.

        Returns:
            A `(polygon_id, radius_used)` tuple, or `None` when no parcel
            with recorded deals exists near the address at any radius.
        """
        results = await self.autocomplete(address)
        if not results:
            logger.info("govmap autocomplete returned no results for %r", address)
            return None

        # Results are score-ordered, so take the best one carrying a
        # usable coordinate.
        point: tuple[float, float] | None = None
        for result in results:
            if isinstance(result, dict):
                point = parse_wkt_point(result.get("shape"))
                if point is not None:
                    break
        if point is None:
            logger.info("No parsable coordinate in govmap results for %r", address)
            return None

        for radius in RADIUS_LADDER:
            polygon_ids = await self.polygons_by_radius(*point, radius)
            if polygon_ids:
                # First entry at the tightest radius that found anything:
                # the closest parcel with recorded transactions.
                return polygon_ids[0], radius

        logger.info("No deal polygon near %r at any radius in %s", address, RADIUS_LADDER)
        return None

    # --- the full workflow -------------------------------------------------

    async def find_comparable_deals(self, address: str) -> list[ComparableDeal]:
        """Full address -> comparable residential transactions pipeline.

        Four calls total, regardless of how dense the area is:

            1. autocomplete(address)              -> point
            2. polygons_by_radius(point, r)       -> the address's parcel
                                                     (repeats up the radius
                                                     ladder only if empty)
            3. street_deals(parcel)          | issued
            4. neighborhood_deals(parcel)    | concurrently

        Measured across 10 Tel Aviv addresses: 4 calls (5 for one that
        needed a wider rung) in 0.6-1.0s, returning 260-435 deals, every
        result a single pure neighbourhood.

        This replaced a version that queried up to 10 polygons and made ~22
        calls in ~2.7s, then needed neighbourhood filtering to undo the
        blending that the wide radius caused. Both filter parameters
        (`only_neighborhood`, `include_own_street`) are gone with it: they
        existed to repair a problem this design does not create.

        Note step 4 alone supplies the bulk of the data — every polygon in
        a neighbourhood returns that neighbourhood's full deal set, so one
        parcel is as good as ten. Step 3 adds sales on the parcel's own
        street, which may run into a neighbouring area; that is deliberate,
        since a street is a coherent micro-market even across a boundary.

        Returns an empty list (never raises) when the address genuinely has
        no data. That is a business "no answer", which the caller turns into
        `outcome="no_comparable_data"`, not an error. Genuine transport
        failures still propagate as the typed tool errors raised by
        `_request`.
        """
        located = await self.find_polygon_for_address(address)
        if located is None:
            return []
        polygon_id, radius = located

        street, neighborhood = await asyncio.gather(
            self.street_deals(polygon_id),
            self.neighborhood_deals(polygon_id),
        )

        # The two endpoints overlap — a sale on this street is also a sale
        # in this neighbourhood — so the same transaction arrives twice.
        # Left in, duplicates would be double-weighted in the median.
        seen: set[tuple] = set()
        deduped: list[ComparableDeal] = []
        for deal in (*street, *neighborhood):
            key = (deal.price, deal.area_sqm, deal.deal_date, deal.street)
            if key not in seen:
                seen.add(key)
                deduped.append(deal)

        logger.info(
            "Found %d unique residential deals for %r (polygon %s at radius %d)",
            len(deduped),
            address,
            polygon_id,
            radius,
        )
        return deduped
