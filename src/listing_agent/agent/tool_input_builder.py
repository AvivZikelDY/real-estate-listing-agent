"""Shared, deterministic construction of a tool call's typed input model.

Single source of truth for turning a validated `TriagePlan`'s `ToolCall`
(plus `geo_resolver`'s resolved output, when available) into the exact
typed input model (`GeoResolverInput` / `NearbyServicesInput` /
`PriceSignalInput`) that tool would be executed with next.

Used by both:
- `evidence_collector.collect_evidence` (the real execution path), and
- `scripts/inspect_triage_plan.py` (a diagnostic path that stops *before*
  any tool is ever executed),

so the two can never silently drift apart.
"""

from src.listing_agent.agent.claim_categories import normalize_category
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, ToolName
from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyServiceCategory,
    NearbyServicesInput,
    PriceSignalInput,
)

ToolInputModel = GeoResolverInput | NearbyServicesInput | PriceSignalInput

# The cities `geo_resolver` is allowed to use as a disambiguation hint:
# Gush Dan and the Sharon strip around it, plus the other major cities.
# Matched as a case-insensitive substring of `raw_address` - never guessed,
# never inferred, and never an extra LLM call.
#
# >>> THIS LIST IS NOW LOAD-BEARING <<<
# A listing whose address matches nothing here does not merely lose a hint:
# `build_tool_input` refuses to run `geo_resolver` at all (see
# `_NO_CITY_HINT_REASON`). A city missing from this list therefore costs a
# whole run, which is why it now covers the metro area rather than the
# eight names it started with.
#
# WHY SUBSTRING AND NOT WORD MATCHING: Hebrew one-letter prefixes attach
# directly to the word ("בתל אביב" = "in Tel Aviv"), so there is no word
# boundary to anchor on. That is also why every name here must be
# distinctive enough not to turn up inside an ordinary word - "אזור" (Azor)
# is deliberately absent, because it is also the everyday word for "area"
# and would match "באזור מוקף גינות" in any listing that has one.
_CITY_NAMES: tuple[str, ...] = (
    # Tel Aviv itself, including the hyphenated spelling govmap returns.
    "תל אביב",
    "תל-אביב",
    "Tel Aviv",
    # Gush Dan
    "רמת גן",
    "Ramat Gan",
    "גבעתיים",
    "Givatayim",
    "בני ברק",
    "Bnei Brak",
    "חולון",
    "Holon",
    "בת ים",
    "Bat Yam",
    "אור יהודה",
    "Or Yehuda",
    "קרית אונו",
    "קריית אונו",
    "Kiryat Ono",
    "גבעת שמואל",
    "Givat Shmuel",
    "גני תקווה",
    "יהוד",
    "Yehud",
    "פתח תקווה",
    "פתח תקוה",
    "Petah Tikva",
    "ראשון לציון",
    "Rishon LeZion",
    # The Sharon strip
    "רמת השרון",
    "Ramat HaSharon",
    "הרצליה",
    "Herzliya",
    "הוד השרון",
    "Hod HaSharon",
    "כפר סבא",
    "Kfar Saba",
    "רעננה",
    "Raanana",
    # South of the metro
    "נס ציונה",
    "Ness Ziona",
    "רחובות",
    "Rehovot",
    # The rest of the country's major cities, kept from the original list.
    "ירושלים",
    "Jerusalem",
    "חיפה",
    "Haifa",
    "באר שבע",
    "Beer Sheva",
)

# Longest first, so a city whose name contains another one ("אור יהודה"
# contains "יהוד") is always tested before the shorter name and wins. The
# secondary alphabetical key exists only to keep the order reproducible.
_KNOWN_CITY_HINTS: tuple[str, ...] = tuple(
    sorted(_CITY_NAMES, key=lambda city: (-len(city), city))
)

# Why `geo_resolver` is not run at all when no city was found.
#
# Without a city, govmap has nothing to disambiguate with and its fuzzy
# search answers from the whole country: measured on a real listing,
# "בשמואל תמיר 2" came back with Be'er Sheva as the best match and three
# more matches in other towns. The tool then either reports `ambiguous`
# (so every dependent tool is skipped anyway) or, worse, resolves
# confidently to a street of the same name in the wrong city.
#
# Guessing the city is not an option - the agent must never introduce a
# fact the listing did not state. So the run stops here and says so: a
# skipped tool carrying "no city was found" is honest evidence, whereas
# coordinates in the wrong city are not.
_NO_CITY_HINT_REASON = (
    "the listing address '{address}' does not name a city this agent recognises, "
    "and a city is never guessed - without one govmap searches the whole country "
    "and answers with a same-named street somewhere else"
)

# Free-text (Hebrew + English) synonyms observed in claim text/planner
# arguments, normalized to the actual `NearbyServiceCategory` values
# `nearby_services` supports (no "kindergarten" enum value exists -
# `"daycare"` is the closest existing category; reused rather than adding
# a near-duplicate).
_NEARBY_CATEGORY_ALIASES: dict[str, NearbyServiceCategory] = {
    "school": "school",
    "schools": "school",
    "בית ספר": "school",
    "בתי ספר": "school",
    "kindergarten": "daycare",
    "kindergartens": "daycare",
    "daycare": "daycare",
    "גן": "daycare",
    "גנים": "daycare",
    "גן ילדים": "daycare",
    # "גני" (regular, non-final nun) is the construct/plural-construct form
    # used in phrases like "מתחם גני ילדים" ("kindergarten complex") -
    # distinct from "גן" (final nun), which never matches as a substring of
    # "גני" because Hebrew uses different letter forms word-finally vs.
    # mid-word.
    "גני": "daycare",
    "גני ילדים": "daycare",
    "מעון": "daycare",
    "מעון יום": "daycare",
    "park": "park",
    "parks": "park",
    "פארק": "park",
    "פארקים": "park",
    "גינה": "park",
    "supermarket": "supermarket",
    "supermarkets": "supermarket",
    "סופרמרקט": "supermarket",
    "pharmacy": "pharmacy",
    "בית מרקחת": "pharmacy",
    "public_transport": "public_transport",
    "transport": "public_transport",
    "תחבורה ציבורית": "public_transport",
    "hospital": "hospital",
    "בית חולים": "hospital",
}


class MissingDependencyError(Exception):
    """Raised when a planned tool call cannot be prepared because a
    prerequisite for it is not available:

    - `nearby_services`/`price_signal` need `geo_resolver`'s resolved
      output and it has not produced one; or
    - `geo_resolver` itself needs a city to search in, and the address
      names none (see `_NO_CITY_HINT_REASON`).

    Kept distinct from `pydantic.ValidationError` (which means "the plan's
    own data is invalid for this tool") - a missing dependency is not an
    invalid plan, it just cannot be fully prepared until an earlier tool
    call actually runs.
    """

    def __init__(self, tool: ToolName, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(f"{tool}: {reason}")


def extract_city_hint(raw_address: str | None) -> str | None:
    """Return the known city named in `raw_address`, if any.

    Purely deterministic substring matching against a fixed list - never
    guessed, never inferred beyond what the address text itself states.

    The match is case-insensitive and the value returned is the list's own
    canonical spelling rather than the caller's: "TEL AVIV" yields
    "Tel Aviv". The comparison used to be case-sensitive despite the
    comment claiming otherwise, which cost little while the hint was
    optional and costs a whole run now that its absence stops one.
    """
    if not raw_address:
        return None
    lowered = raw_address.lower()
    for city in _KNOWN_CITY_HINTS:
        if city.lower() in lowered:
            return city
    return None


def normalize_nearby_categories(raw_categories: object) -> list[NearbyServiceCategory]:
    """Normalize a planner-supplied `categories` argument to valid
    `NearbyServiceCategory` values, deduplicated and order-preserving.

    Unrecognized entries are dropped rather than raising - a partially
    understood category list is still better than falling back to every
    category. Returns an empty list if `raw_categories` isn't a usable
    list or nothing in it is recognized (caller falls back to the input
    model's own default in that case).
    """
    if not isinstance(raw_categories, list):
        return []
    normalized: list[NearbyServiceCategory] = []
    for item in raw_categories:
        if not isinstance(item, str):
            continue
        category = _NEARBY_CATEGORY_ALIASES.get(item.strip().lower())
        if category is not None and category not in normalized:
            normalized.append(category)
    return normalized


# Fixed, deterministic scan order used when extracting category mentions
# from free text (a planner `query` argument, or a claim's own text) -
# always this order regardless of the order words appear in the source
# text, so results are reproducible across runs/wording (Phase 19 fix for
# "an explicit park-only question must not silently expand into every
# category").
_NEARBY_CATEGORY_SCAN_ORDER: tuple[NearbyServiceCategory, ...] = (
    "school",
    "daycare",
    "park",
    "supermarket",
    "pharmacy",
    "public_transport",
    "hospital",
)


def _extract_categories_from_text(text: str) -> list[NearbyServiceCategory]:
    """Deterministically extract every known nearby-service category
    mentioned in free text (Hebrew or English aliases from
    `_NEARBY_CATEGORY_ALIASES`), in the fixed `_NEARBY_CATEGORY_SCAN_ORDER`.

    Never a heuristic guess beyond literal alias substrings already used
    elsewhere in this module - no new vocabulary, no LLM call.
    """
    lowered = text.strip().lower()
    if not lowered:
        return []
    found: list[NearbyServiceCategory] = []
    for category in _NEARBY_CATEGORY_SCAN_ORDER:
        aliases = (alias for alias, value in _NEARBY_CATEGORY_ALIASES.items() if value == category)
        if any(alias in lowered for alias in aliases):
            found.append(category)
    return found


def resolve_nearby_categories(
    *,
    categories: object = None,
    query: object = None,
    claim_text: str | None = None,
) -> list[NearbyServiceCategory]:
    """Deterministically resolve which `nearby_services` categories a call
    should use, in a fixed priority order (Phase 19 - closes the "planner
    used `query` instead of `categories`" deviation):

    1. valid explicit `categories` (already alias-normalized);
    2. a planner `query` argument (e.g. a single free-text term the
       planner used instead of a proper `categories` list);
    3. the text of the linked canonical `nearby_amenities` claim;
    4. `[]` - the caller falls back to `NearbyServicesInput`'s own default
       (every supported category), which is only appropriate for a
       genuinely broad "what's nearby" request, never a narrow one that
       merely used an unexpected argument name.

    Unknown/unrecognized values at every tier are dropped rather than
    silently expanding to every category - the caller only widens to the
    full default set when *no* tier produced a recognized value at all.
    """
    normalized = normalize_nearby_categories(categories)
    if normalized:
        return normalized

    if isinstance(query, str) and query.strip():
        found = _extract_categories_from_text(query)
        if found:
            return found

    if claim_text:
        found = _extract_categories_from_text(claim_text)
        if found:
            return found

    return []


# Fixed, deterministic priority order of alternate free-text argument names
# a planner may use instead of `query` (observed during real-LLM
# verification, e.g. `service_type`) to describe what nearby category it
# wants. Never a substitute for `categories` - only consulted by
# `extract_nearby_query_argument` when `query` itself is absent.
_NEARBY_QUERY_ARGUMENT_ALIASES: tuple[str, ...] = (
    "query",
    "service_type",
    "category",
    "amenity_type",
    "amenity",
    "type",
)


def extract_nearby_query_argument(arguments: dict[str, object]) -> str | None:
    """Return the first usable free-text "what nearby category" value from
    a planner call's raw arguments, checking `_NEARBY_QUERY_ARGUMENT_ALIASES`
    in a fixed order (Phase 19 - the real planner has been observed using
    `service_type` as well as `query` for this).

    Returns `None` if no alias key holds a non-empty string - the caller
    then falls through to the linked claim's text, per
    `resolve_nearby_categories`'s own priority chain.
    """
    for key in _NEARBY_QUERY_ARGUMENT_ALIASES:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# Fixed, deterministic priority order of alternate list-valued argument
# names a planner may use instead of `categories` (observed during
# real-LLM verification, e.g. `services`) to list several nearby
# categories at once. Never a semantic reinterpretation of what the
# planner asked for - only an argument-name normalization, exactly like
# `categories` itself.
_NEARBY_CATEGORIES_ARGUMENT_ALIASES: tuple[str, ...] = (
    "categories",
    "services",
    "service_types",
    "amenity_types",
)


def extract_nearby_categories_argument(arguments: dict[str, object]) -> object:
    """Return the first usable list-valued "categories" argument from a
    planner call's raw arguments, checking
    `_NEARBY_CATEGORIES_ARGUMENT_ALIASES` in a fixed order (Phase 19 - the
    real planner has been observed using `services` as well as
    `categories` for this).

    Returns `None` if no alias key holds a list - the caller then falls
    through to `query`/claim-text tiers, per
    `resolve_nearby_categories`'s own priority chain.
    """
    for key in _NEARBY_CATEGORIES_ARGUMENT_ALIASES:
        value = arguments.get(key)
        if isinstance(value, list):
            return value
    return None


def _linked_nearby_claim_text(call: ToolCall, claims: list[Claim] | None) -> str | None:
    """Return the combined text of every claim `call` is linked to, if any.

    Prefers explicit `call.supports_claim_ids` matches (tier 1, same
    priority `evidence_mapper.claim_linking` uses) - combining *all* of
    them, not just the first, since a single `nearby_services` call may
    legitimately support several atomized `nearby_amenities` claims (e.g.
    "green view" + "kindergarten complex" + "street parking" split from
    one compound sentence) and only the combined text reliably recovers
    every mentioned category. Otherwise falls back to combining every
    claim in `claims` already classified with the canonical
    `"nearby_amenities"` category. Returns `None` (never guesses) when
    `claims` is not supplied or nothing matches.
    """
    if not claims:
        return None

    if call.supports_claim_ids:
        by_id = {claim.id: claim for claim in claims if claim.id}
        matched_texts = [
            by_id[claim_id].text for claim_id in call.supports_claim_ids if claim_id in by_id
        ]
        if matched_texts:
            return " ".join(matched_texts)

    nearby_texts = [
        claim.text for claim in claims if normalize_category(claim.category) == "nearby_amenities"
    ]
    return " ".join(nearby_texts) if nearby_texts else None


def build_tool_input(
    call: ToolCall,
    property_details: PropertyDetails,
    geo_output: GeoResolverOutput | None,
    claims: list[Claim] | None = None,
) -> ToolInputModel:
    """Build the exact typed input `call.tool` would be executed with next.

    Args:
        call: The planned (validated) tool call.
        property_details: The plan's extracted listing facts.
        geo_output: `geo_resolver`'s resolved output so far in this run, or
            `None` if `geo_resolver` has not (yet) produced a `"resolved"`
            outcome - including when it has not run at all, e.g. in
            `scripts/inspect_triage_plan.py`, which never executes any
            tool.
        claims: The plan's claims (Phase 19), used only by `nearby_services`
            to recover a narrow category list from the linked claim's text
            when the planner's own arguments don't provide usable
            categories (see `resolve_nearby_categories`). Optional and
            defaults to `None` for backward compatibility with existing
            callers; omitting it simply means that fallback tier is
            unavailable, never an error.

    Raises:
        pydantic.ValidationError: The plan's own data is invalid for this
            tool (e.g. a negative `asking_price`). Callers report this as
            a structured, never-retryable `"invalid_input"` failure.
        MissingDependencyError: a prerequisite is missing - either
            `call.tool` is location-dependent (`nearby_services`,
            `price_signal`) and `geo_output` is `None`, or it is
            `geo_resolver` and the address names no recognised city.
            Callers report this as a `"skipped"` (not `"error"`) evidence
            entry, or as an explicit unresolved-dependency placeholder in
            diagnostic output.
        ValueError: `call.tool` is outside the whitelist. Should not
            happen for a plan already checked by `plan_validator`.
    """
    if call.tool == "geo_resolver":
        raw_address = property_details.raw_address or ""
        city_hint = extract_city_hint(raw_address)
        # No recognised city means no run at all: see `_NO_CITY_HINT_REASON`.
        # An empty address is left to the schema's own `min_length`, because
        # "there is no address" is a different thing to report than "there
        # is an address and it names no city".
        if raw_address and city_hint is None:
            raise MissingDependencyError(
                call.tool, _NO_CITY_HINT_REASON.format(address=raw_address)
            )
        return GeoResolverInput(address=raw_address, city_hint=city_hint)

    if call.tool == "nearby_services":
        if geo_output is None:
            raise MissingDependencyError(
                call.tool, "geo_resolver did not produce resolved coordinates"
            )
        categories = resolve_nearby_categories(
            categories=extract_nearby_categories_argument(call.arguments),
            query=extract_nearby_query_argument(call.arguments),
            claim_text=_linked_nearby_claim_text(call, claims),
        )
        if categories:
            return NearbyServicesInput(
                lat=geo_output.lat, lon=geo_output.lon, categories=categories
            )
        return NearbyServicesInput(lat=geo_output.lat, lon=geo_output.lon)

    if call.tool == "price_signal":
        if geo_output is None:
            raise MissingDependencyError(
                call.tool, "geo_resolver did not produce a resolved address"
            )
        return PriceSignalInput(
            normalized_address=geo_output.normalized_address,
            neighborhood=geo_output.neighborhood,
            listing_type=property_details.listing_type,
            asking_price=property_details.asking_price,
            area_sqm=property_details.sqm,
        )

    raise ValueError(f"Tool '{call.tool}' is not supported by build_tool_input.")


__all__ = [
    "MissingDependencyError",
    "ToolInputModel",
    "build_tool_input",
    "extract_city_hint",
    "extract_nearby_categories_argument",
    "extract_nearby_query_argument",
    "normalize_nearby_categories",
    "resolve_nearby_categories",
]
