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

>>> WHY THIS FILE IS NOT CALLED YET <<<
---------------------------------------
Everything here is finished, working code, verified against the live API —
but `NirPriceSignal.execute()` does not call it yet.

MEASURED LATENCY: a full `find_comparable_deals()` for a Tel Aviv address
takes **2.6-2.9 seconds** end to end (~450 deals returned), against the 8s
ceiling `LocalToolExecutor` enforces
(`src/listing_agent/agent/tool_executor/local.py:36`).

So it FITS, with roughly 5 seconds of headroom. The earlier assumption that
~21 throttled calls could not finish in time was wrong: deduplicating
polygons and dropping empty ones cuts the count well below the worst case,
and steps 3 and 4 run concurrently per polygon.

Staying on the offline snapshot is therefore a deliberate choice, not a
forced one. What it buys:

  * Deterministic tests, with no network in CI.
  * `price_signal` keeps working when govmap is down or slow.
  * No per-request dependency on a free public service's latency, which is
    the part of the 5s headroom nobody controls.

What it costs: the figures are a point-in-time extract, which the tool
discloses in `limitations` on every result.

Going live is a one-line change — search `SWAP:` in `price_signal.py`.
Either way this client is exercised for real every time the snapshot is
regenerated, so it cannot rot silently.
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

# Search radius passed to the polygon lookup.
#
# NOTE these are Web Mercator units, not true ground metres — they are
# inflated by ~1.18x at Tel Aviv's latitude (see `coordinates.py`), so 300
# here is roughly 254 real metres. Deliberately kept tight: the point is
# comparable properties, and widening the net pulls in genuinely different
# micro-markets a few streets away.
DEFAULT_RADIUS = 300

# Cap on polygons queried per address. Each one costs 2 HTTP calls, so this
# is the single biggest lever on total latency.
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
    def _street_from_result_id(result_id: object) -> str | None:
        """Pull the street name out of an autocomplete result's `id`.

        The `id` is pipe-delimited and structured, e.g.

            "address|ADDR|53785356|רוטשילד|30|תל אביב"
             0       1    2        3        4  5

        so index 3 is the street and index 4 the house number. This is far
        more reliable than trying to parse the free-text address, which
        varies in punctuation and word order.

        Note the polygon metadata from step 2 is NOT a usable alternative:
        measured, only ~33% of polygons carry a `streetNameHeb` at all, and
        for "רוטשילד 30" none of them was actually רוטשילד. The autocomplete
        id is the only dependable source for the queried street.

        Returns `None` when the id is missing or not in the expected shape.
        """
        if not isinstance(result_id, str):
            return None
        parts = result_id.split("|")
        if len(parts) < 4:
            return None
        street = parts[3].strip()
        return street or None

    @staticmethod
    def _normalize_name(value: str | None) -> str | None:
        """Case- and whitespace-insensitive form used for name comparisons."""
        if not value:
            return None
        return " ".join(value.strip().lower().split())

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

    async def find_comparable_deals(
        self,
        address: str,
        radius: int = DEFAULT_RADIUS,
        only_neighborhood: str | None = None,
        include_own_street: bool = True,
    ) -> list[ComparableDeal]:
        """Full address -> comparable residential transactions pipeline.

        This is the method `price_signal` will call once the executor
        timeout allows it, chaining all four endpoints described in the
        module docstring.

        Steps 3 and 4 are issued concurrently per polygon via
        `asyncio.gather` — the one meaningful latency win available without
        dropping data, since it roughly halves the deal-fetching phase.

        >>> A SEARCH RADIUS CAN STRADDLE TWO NEIGHBOURHOODS <<<

        Each individual polygon belongs to exactly one neighbourhood, but a
        300-unit radius does not. Measured example: a search around
        "אילת 30 תל אביב" returns 10 polygons that split 5/5 between
        צפון יפו (348 deals) and נווה צדק (260 deals) — genuinely different
        markets with different price levels.

        Left unfiltered, the returned list blends both, and any median over
        it describes neither.

        THE SCOPING RULE
        ----------------
        A deal is kept when EITHER:

          * it is in the address's own neighbourhood (`only_neighborhood`,
            normally what `geo_resolver` resolved), OR
          * it is on the address's own street (`include_own_street`), even
            where that street runs on into a different neighbourhood.

        The street clause is the point: a street is a coherent micro-market
        in a way a neighbourhood boundary is not, so sales further along it
        stay relevant even after the boundary is crossed. Measured yields
        for the street clause: 10 deals on רוטשילד, 23 on אילת, 32 on
        דיזנגוף, 40 on פלורנטין.

        Filtering happens after fetching, not before, because polygons
        cannot be pre-selected: the step-2 metadata carries no neighbourhood
        at all, and only ~33% of polygons carry any street name (for
        "רוטשילד 30", none of them was רוטשילד). The only way to learn where
        a polygon belongs is to call it.

        Args:
            address: Free-text address to search around.
            radius: Search radius in Web Mercator units (see `coordinates.py`).
            only_neighborhood: Keep deals in this neighbourhood (compared
                case- and whitespace-insensitively). When `None`, no
                neighbourhood restriction is applied at all and every deal
                found is returned, blended neighbourhoods included.
            include_own_street: Also keep deals on the queried address's own
                street regardless of their neighbourhood. Has no effect
                unless `only_neighborhood` is set, since without it nothing
                is being filtered out in the first place.

        Returns an empty list (never raises) when the address simply has no
        data. That is a business "no answer", which the caller turns into
        `outcome="no_comparable_data"`, not an error. Genuine transport
        failures still propagate as the typed tool errors raised by
        `_request`.
        """
        # Step 1: address -> candidates.
        results = await self.autocomplete(address)
        if not results:
            logger.info("govmap autocomplete returned no results for %r", address)
            return []

        # Take the first candidate carrying a usable coordinate. Results
        # come back score-ordered, so the first is the best match. Capture
        # its street at the same time — it defines the street clause of the
        # scoping rule, and this is the only reliable place to get it.
        point: tuple[float, float] | None = None
        own_street: str | None = None
        for result in results:
            if isinstance(result, dict):
                point = parse_wkt_point(result.get("shape"))
                if point is not None:
                    own_street = self._street_from_result_id(result.get("id"))
                    break
        if point is None:
            logger.info("No parsable coordinate in govmap results for %r", address)
            return []

        # Step 2: coordinate -> polygon ids. Web Mercator metres, not lat/lon.
        x, y = point
        polygon_ids = await self.polygons_by_radius(x, y, radius)
        if not polygon_ids:
            logger.info("No deal polygons within %d units of %r", radius, address)
            return []

        # Steps 3 & 4: polygon ids -> deals.
        all_deals: list[ComparableDeal] = []
        for polygon_id in polygon_ids:
            street, neighborhood = await asyncio.gather(
                self.street_deals(polygon_id),
                self.neighborhood_deals(polygon_id),
            )
            all_deals.extend(street)
            all_deals.extend(neighborhood)

        # The two endpoints overlap heavily — a street deal is also a
        # neighbourhood deal, and adjacent polygons share a neighbourhood —
        # so the same sale arrives many times over. Without deduplication
        # those duplicates would be counted repeatedly and skew the median
        # toward whichever streets happen to have the most polygons.
        seen: set[tuple] = set()
        deduped: list[ComparableDeal] = []
        for deal in all_deals:
            key = (deal.price, deal.area_sqm, deal.deal_date, deal.street)
            if key not in seen:
                seen.add(key)
                deduped.append(deal)

        # Apply the scoping rule: the address's own neighbourhood, plus its
        # own street wherever that street happens to run. Without this a
        # boundary search silently blends two markets into one median.
        if only_neighborhood is not None:
            wanted_neighborhood = self._normalize_name(only_neighborhood)
            wanted_street = self._normalize_name(own_street) if include_own_street else None

            scoped = [
                d
                for d in deduped
                if self._normalize_name(d.neighborhood) == wanted_neighborhood
                or (wanted_street is not None and self._normalize_name(d.street) == wanted_street)
            ]
            logger.info(
                "Scoped %d deals to %d (neighbourhood=%r, street=%r)",
                len(deduped),
                len(scoped),
                only_neighborhood,
                own_street if include_own_street else None,
            )
            deduped = scoped

        logger.info("Found %d unique residential deals for %r", len(deduped), address)
        return deduped
