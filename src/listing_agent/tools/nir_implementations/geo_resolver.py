"""Nir's real `geo_resolver` implementation, backed by govmap autocomplete.

WHAT THIS TOOL DOES
-------------------
Takes a free-text address as a human wrote it ("רוטשילד 30 תל אביב", or
something vaguer like "דיזנגוף") and decides whether it points at exactly
one real place. If it does, it returns the canonical form, WGS84
coordinates, and the neighbourhood. If it doesn't, it says so honestly
rather than guessing.

>>> WHY THIS ONE IS LIVE, LIKE THE OTHER TWO <<<
--------------------------------------------------
All three tools (`geo_resolver`, `nearby_services`, `price_signal`) are
live-only for a real runtime request today - none of them read a snapshot
file to answer. `geo_resolver` in particular could never have used one:

  * `price_signal` needs AGGREGATE data over a FINITE set of
    neighbourhoods - a snapshot would at least be structurally possible
    there (an offline extract still exists for tests/reference, see
    `govmap_client.py`, though the live path never reads it).
  * `geo_resolver` needs a LOOKUP over an INFINITE key space — every
    address anyone might type, including misspellings. A pre-fetched
    gazetteer would answer `not_found` for anything not on the list, which
    is worse than useless: it would silently turn real addresses into
    failures.

It is also cheap: the common path is a single HTTP call (~0.3s) against
the 8s `LocalToolExecutor` ceiling.

THE RESULT CONTRACT (docs/nir_handoff.md)
-----------------------------------------
`ToolResult.status` says whether the *tool* worked; `outcome` says what it
*found*. "We looked and this address is not real" is a successful run.

    status="ok" + outcome="resolved"    -> one confident match
    status="ok" + outcome="ambiguous"   -> several plausible, none dominant
    status="ok" + outcome="not_found"   -> no match at all, or nothing that
                                           scored well enough to believe
    status="error"                      -> the tool itself broke

TWO INDEPENDENT CONFIDENCE CHECKS
---------------------------------
A match must pass BOTH before it is reported as `resolved`:

  1. CONTENT  (`_HOUSE_NUMBER_RE`)      - did we get back the house number
                                          we actually asked for?
  2. RELATIVE (`_DOMINANT_SCORE_RATIO`) - is it clearly better than the
                                          alternatives?

Only having (2) was a real bug: govmap's fuzzy search always returns its
best guesses and never says "no match", so when the type filter removed
every rival, a junk match was left alone in the list and (2) waved it
through by definition. Note that (1) deliberately is NOT a minimum score —
see `_HOUSE_NUMBER_RE` for why that approach fails.

WHEN THE HOUSE NUMBER IS NOT IN THE INDEX
-----------------------------------------
Failing (1) does not always mean "this address is not real". govmap's
address index is incomplete for new construction: measured live,
"שמואל תמיר תל אביב" resolves as a street at score 4592.9, and the index
holds numbers 1, 3, 9, 13, 15, 17, 23, 27, 29, 31, 33, 37, 45 and 47 on
it — but not 2, the number an actual listing on that street advertises.
Reporting `not_found` there was strictly worse than useless: it also
skipped `nearby_services` and `price_signal`, so a real listing came back
with nothing checked at all, while dropping the house number from the very
same query resolved fine. Adding information made the answer worse.

So a query whose house number is missing from the match falls back ONCE to
the same query without the number (`_street_level_match`). The fallback is
deliberately narrow, because it re-opens the exact hole (1) was added to
close — a junk query would otherwise land on a real street:

  * the fallback match must be `type="street"` (not a settlement or a
    neighbourhood: we asked for a specific street, and anything coarser
    means the street itself was not found);
  * it must still be dominant among the streets returned;
  * every word of the queried name must actually appear in it
    (`_street_name_matches`), which is what still rejects
    "כתובת לא קיימת 999 תל אביב" -> "הקרן קיימת" — they share only the
    word "קיימת".

A successful fallback is reported as `resolved` at `approximate` quality
with the missing house number stated in `limitations`, never as `exact`.
"""

import logging
import re

from src.listing_agent.tools.nir_implementations.coordinates import (
    parse_wkt_point,
    web_mercator_to_wgs84,
)
from src.listing_agent.tools.nir_implementations.govmap_client import GovmapClient
from src.listing_agent.tools.schemas import GeoResolverInput, GeoResolverOutput, ToolResult

logger = logging.getLogger(__name__)

_SOURCE = "govmap.gov.il (Israeli government address search)"

# --- Resolution tuning -----------------------------------------------------

# How much the best match must outscore the runner-up to count as a single
# confident answer rather than an ambiguous set.
#
# Calibrated against real API responses:
#   "שבזי 20 תל אביב"  -> 4725 vs 515   (9.2x)  clearly one answer
#   "הרצל תל אביב"     -> 3948 vs 3581  (1.1x)  genuinely ambiguous
#   "דיזנגוף"          -> six results ALL at 1358.9 across different
#                          cities (1.0x) - the API itself cannot choose
#
# 2.0 sits comfortably between the two clusters.
_DOMINANT_SCORE_RATIO = 2.0

# Any run of 1-4 digits: a house number in a query, or in a matched name.
#
# >>> WHY NOT AN ABSOLUTE SCORE FLOOR <<<
# The obvious guard against a weak match is a minimum score, and it does not
# work: govmap's score is RELATIVE TO THE QUERY STRING, not a measure of how
# good the match is. Measured on the live API, the identical correct match
# for Rothschild 30 scores
#     "רוטשילד 30 תל אביב"          -> 4508   (clean)
#     "ברחוב רוטשילד 30 בתל אביב"   ->  236   (as triage_planner extracts it)
# while the junk match that motivated this check scores 202. Real addresses
# and nonsense therefore OVERLAP on score, and any floor high enough to
# reject the junk also rejects real listings — a floor of 1000 was tried and
# broke Rothschild 30 end to end.
#
# The house number is the signal that actually separates them. govmap's
# fuzzy search matches on words, so nonsense text can land on a real STREET
# ("כתובת לא קיימת 999" -> "הקרן קיימת", on the shared word "קיימת"), but
# the number we asked for is simply absent from what came back. A query that
# names a house number and gets a match without it did not find that
# address.
_HOUSE_NUMBER_RE = re.compile(r"\d{1,4}")

# A token that is nothing but a house number, e.g. "2" or the "2," that
# survives when a listing writes "שמואל תמיר 2, תל אביב". Dropped from the
# query by `_street_level_match` to ask about the street alone.
_DIGIT_ONLY_TOKEN_RE = re.compile(r"^[\W_]*\d{1,4}[\W_]*$")

# Hebrew one-letter prefixes that attach directly to the following word
# ("בשמואל" = "at Shmuel"). These are NEVER stripped from a query — doing so
# would corrupt street names that genuinely begin with those letters
# ("ביאליק" -> "יאליק"). They are stripped only when COMPARING two names in
# `_street_name_matches`, where both spellings are already in hand and
# nothing is being guessed. This matters: `triage_planner` copies a listing
# verbatim, so the address arrives as "בשמואל תמיר 2" while govmap's own
# spelling is "שמואל תמיר" — the same street, one letter apart.
_HEBREW_ONE_LETTER_PREFIXES = frozenset("בהלמושכ")

# Shortest word worth comparing. Below this a "match" is mostly noise.
_MIN_COMPARABLE_TOKEN_LEN = 2

# Only strip a prefix from a word long enough to still be a word without it.
_MIN_PREFIXED_TOKEN_LEN = 4

# Structural filler dropped from the query before it is sent.
#
# `triage_planner` extracts an address as a human wrote it — "ברחוב רוטשילד
# 30 בתל אביב" — and the word "רחוב"/"street" actively harms the search,
# because govmap scores on word overlap and that word appears in no street
# NAME. Measured on the live API:
#
#   "ברחוב רוטשילד 30 בתל אביב" -> 10 matches, top 235.8, runner-up 211.1
#                                  (Rothschild 30 in Ashdod and Kfar Saba)
#                                  ratio 1.12 -> reported AMBIGUOUS
#   "רוטשילד 30 בתל אביב"       ->  1 match,  608.1  -> resolves cleanly
#
# So the filler both depressed the score and dragged in same-numbered
# addresses from other cities, turning a perfectly ordinary listing into an
# unusable "ambiguous" and skipping every dependent tool.
#
# ONLY this structural word is removed. Hebrew one-letter prefixes ("ב" in
# "בתל אביב") are deliberately left alone: they attach directly to the next
# word, so stripping them would corrupt real street names that simply begin
# with those letters ("ביאליק" -> "יאליק"). "שדרות" is likewise left alone —
# it is also a real city (Sderot).
_FILLER_TOKENS = frozenset({"רחוב", "ברחוב", "רח'", "ברח'"})

# How many alternatives to report when the outcome is ambiguous. Enough to
# be useful to a reader, few enough to stay readable.
_MAX_CANDIDATES = 5

# govmap's result `type` maps directly onto our resolution quality, because
# it says how precisely the query was matched:
#   "address"      -> pinned to a specific building
#   "street"       -> the right street, but no house number
#   "neighborhood" -> only the general area
#   "settlement"   -> only the town
_QUALITY_BY_TYPE: dict[str, str] = {
    "address": "exact",
    "street": "approximate",
    "neighborhood": "low_confidence",
    "settlement": "low_confidence",
    "poi": "approximate",
}

# Only these result types describe a PLACE SOMEONE CAN LIVE AT. Anything
# else govmap indexes is not an address and must never be returned as one.
#
# This filter is load-bearing. Searching the nonsense address
# "כתובת לא קיימת 999 תל אביב" returns three matches — two
# `type="transportation"` entries both literally named "999", plus
# `type="ways"` for "כביש 999" (Highway 999). Without this filter the tool
# reported that gibberish as an `ambiguous` address with candidates
# ["999", "999"], instead of the honest `not_found`.
_ADDRESSABLE_TYPES = frozenset(_QUALITY_BY_TYPE)

# Stated on every street-level fallback, because a reader must never take
# these coordinates for the building's own position: govmap's street point
# is the mid-point of the whole street (its own id calls it
# STREET_MID_POINT), which on a long street is a considerable distance from
# any given number.
_MISSING_HOUSE_NUMBER_NOTE = (
    "House number {numbers} is not in the government address index for this street, "
    "so the position is the street itself rather than the building; the street was "
    "matched, the specific building was not."
)

_STRAIGHT_LINE_NOTE = (
    "Coordinates are the centre point govmap records for the matched address, "
    "not a surveyed position of the building itself."
)


class NirGeoResolver:
    """Real `geo_resolver` tool backed by the govmap address index."""

    # Must match the whitelisted tool name exactly — the registry looks the
    # tool up by this string (`tools/registry.py`).
    name = "geo_resolver"

    def __init__(
        self,
        client_factory: type[GovmapClient] = GovmapClient,
        resolve_neighborhood: bool = True,
    ) -> None:
        """
        Args:
            client_factory: Injectable so tests can substitute a stub and
                never touch the network. Defaults to the real client.
            resolve_neighborhood: Whether to spend two extra HTTP calls
                discovering the neighbourhood name (see
                `_lookup_neighborhood`). On by default because
                `price_signal` depends on it, but switchable for callers
                that only need coordinates.
        """
        self._client_factory = client_factory
        self._resolve_neighborhood = resolve_neighborhood

    async def execute(self, input_model: GeoResolverInput) -> ToolResult[GeoResolverOutput]:
        """Resolve an address.

        Never raises. Every failure path returns a `ToolResult` instead —
        the outermost `try` guarantees it, because `evidence_collector`
        must survive any single tool misbehaving.
        """
        try:
            # A city hint is folded into the query text rather than sent as
            # a separate parameter, because govmap has no city field — its
            # search is one free-text box. Appending it is what turns the
            # hopeless "דיזנגוף" (six equal matches across six cities) into
            # something resolvable.
            query = self._strip_filler(input_model.address)

            hint = (input_model.city_hint or "").strip()
            if hint:
                # "בתל אביב" -> "תל אביב". Removing a one-letter Hebrew
                # prefix is unsafe in general (it would maul street names
                # beginning with the same letter), but it is safe HERE
                # because `city_hint` tells us the exact string the city is,
                # so nothing is guessed. Measured: "ברחוב דיזנגוף 100 בתל
                # אביב" is ambiguous (Dizengoff 100 exists in several
                # cities) while "דיזנגוף 100 תל אביב" scores 4886 and
                # resolves outright.
                query = query.replace(f"ב{hint}", hint)
                if hint.lower() not in query.lower():
                    query = f"{query} {hint}"

            async with self._client_factory() as client:
                # The listing's own wording is always asked first.
                result = await self._search(client, query, input_model.address)
                if self._is_resolved(result):
                    return result

                # It did not resolve. If the street name carries a Hebrew
                # one-letter preposition, ask again without it - see
                # `_without_leading_prefix`. Anything that comes back must
                # name the same street as the original query, so this can
                # correct a spelling without ever changing the question.
                alternate = self._without_leading_prefix(query)
                if alternate is None:
                    return result

                logger.info(
                    "%r did not resolve; retrying without the leading prefix as %r",
                    query,
                    alternate,
                )
                retry = await self._search(
                    client, alternate, input_model.address, verify_against=query
                )
                # The faithful query's own outcome is what gets reported if
                # the retry fails too: it is the honest answer to what the
                # listing actually said, and `ambiguous` carries candidates
                # a reader can use, where the retry's `not_found` carries
                # nothing.
                return retry if self._is_resolved(retry) else result

        except Exception as exc:
            # Last-resort safety net: an unreachable data source or any
            # unforeseen bug becomes a structured error, never an exception
            # escaping into the agent pipeline.
            logger.exception("geo_resolver failed for %r", input_model.address)
            return ToolResult(
                status="error",
                error=f"geo_resolver could not complete: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _is_resolved(result: ToolResult[GeoResolverOutput]) -> bool:
        return result.data is not None and result.data.outcome == "resolved"

    @staticmethod
    def _without_leading_prefix(query: str) -> str | None:
        """`query` with a Hebrew one-letter preposition taken off its first
        word, or `None` when there is nothing to take off.

        "בשמואל תמיר 2" -> "שמואל תמיר 2". The letter means "in/at" and is
        written joined to the noun, so a listing that says where a flat IS
        hands us a street name that does not exist. govmap scores on word
        overlap and is unforgiving about it - measured live:

            "בשמואל תמיר 2 תל אביב" -> 9 rows for the WRONG street ("תמיר"),
                                       top score 225.5
            "שמואל תמיר 2 תל אביב"  -> 9 rows for the right street,
                                       top score 275.6
            "בשמואל תמיר תל אביב"   -> no street row at all
            "שמואל תמיר תל אביב"    -> the street, at 4592.9

        This is why the query is never rewritten in place: "ביאליק" begins
        with the same letter and is a real street name, so stripping it
        blindly searches for a street that does not exist. The stripped form
        is only ever an ADDITIONAL attempt, made after the faithful one
        failed, and whatever it returns is checked back against the original
        wording by `_street_name_matches`. Confirmed on that exact case -
        govmap answers "יאליק רמת גן" with ביאליק addresses and no street,
        so the attempt yields nothing rather than anything wrong.
        """
        tokens = query.split()
        if not tokens:
            return None
        first = tokens[0]
        if len(first) < _MIN_PREFIXED_TOKEN_LEN or first[0] not in _HEBREW_ONE_LETTER_PREFIXES:
            return None
        return " ".join([first[1:], *tokens[1:]])

    async def _search(
        self,
        client: GovmapClient,
        query: str,
        address: str,
        *,
        verify_against: str | None = None,
    ) -> ToolResult[GeoResolverOutput]:
        """One full search: ask govmap `query`, and decide what came back.

        Args:
            query: The exact text sent to govmap.
            address: The caller's original address, used only for the
                `not_found` message a reader sees.
            verify_against: When set, the match must also NAME the same
                place as this text (`_street_name_matches`) before it counts
                as resolved. Set on the prefix-stripped retry, where the
                question asked is no longer exactly the one the listing
                asked; left unset for the faithful query, whose whole value
                is govmap's fuzzy matching - it is what turns the typo
                "דיזנגוג 50" into "דיזנגוף 50", and a name check there would
                reject that.
        """
        results = await client.autocomplete(query)

        # --- no match at all: a successful "this is not real" -----
        if not results:
            return self._not_found(address)

        # --- keep only things that are actually addresses --------
        # govmap indexes roads, junctions and transport features in
        # the same search. They are not places to live, and letting
        # them through turns nonsense queries into confident-looking
        # answers. See `_ADDRESSABLE_TYPES`.
        usable = [
            r
            for r in results
            if isinstance(r, dict) and str(r.get("type", "")).lower() in _ADDRESSABLE_TYPES
        ]
        if not usable:
            return self._not_found(address)

        best = usable[0]

        # --- did we get back the house number we asked for? ------
        # Checked before dominance, because dominance is only a
        # RELATIVE test: once the type filter removes every rival, a
        # lone junk match has nothing to lose to and is waved through
        # by definition. See `_HOUSE_NUMBER_RE`.
        # A missing house number is not automatically a fake
        # address — govmap's index simply does not carry every
        # building. One narrow fallback asks about the street
        # alone; see the module docstring for why it cannot be
        # any wider than it is.
        missing_number_note: str | None = None
        if not self._house_number_matches(query, best):
            street = await self._street_level_match(client, query, verify_against or query)
            if street is None:
                logger.info(
                    "govmap best match %r for %r lacks the requested house number "
                    "and no street-level match backs it up; reporting not_found",
                    best.get("text"),
                    query,
                )
                return self._not_found(address)
            logger.info(
                "house number missing from the index for %r; falling back to the "
                "street-level match %r",
                query,
                street.get("text"),
            )
            best = street
            missing_number_note = _MISSING_HOUSE_NUMBER_NOTE.format(
                numbers=", ".join(sorted(set(_HOUSE_NUMBER_RE.findall(query))))
            )
        else:
            # Dominance is only meaningful for the ORIGINAL result set:
            # the fallback ran its own dominance check over the streets
            # it got back, and `usable` describes a different query.
            if not self._is_dominant(usable):
                return self._ambiguous(usable)
            # A rewritten query has to prove it landed on the place the
            # listing named; the faithful query does not, because fuzzy
            # matching is exactly what it is there for.
            if verify_against and not self._street_name_matches(
                verify_against, str(best.get("text") or "")
            ):
                logger.info(
                    "retry match %r does not name the same place as %r; discarding it",
                    best.get("text"),
                    verify_against,
                )
                return self._not_found(address)

        # --- one confident match: extract coordinates ------------
        point = parse_wkt_point(best.get("shape"))
        if point is None:
            # Matched a name but got no usable position. There is
            # nothing to act on, so this is honestly "not found"
            # rather than a fabricated coordinate.
            logger.info("govmap match for %r carried no parsable point", query)
            return self._not_found(address)

        # THE CONTRACT BOUNDARY: govmap works in Web Mercator
        # metres, the schema requires WGS84 degrees. This is the one
        # place the conversion is needed.
        lat, lon = web_mercator_to_wgs84(*point)

        neighborhood = None
        if self._resolve_neighborhood:
            neighborhood = await self._lookup_neighborhood(client, point)

        return self._resolved(
            best, lat, lon, neighborhood, extra_limitation=missing_number_note
        )


    # --- decision logic ----------------------------------------------------

    @staticmethod
    def _strip_filler(address: str) -> str:
        """Drop structural filler words from an address before searching.

        See `_FILLER_TOKENS` for what is removed and, more importantly, what
        deliberately is not. Falls back to the original text if stripping
        would leave nothing to search for.
        """
        tokens = [token for token in address.split() if token not in _FILLER_TOKENS]
        return " ".join(tokens) if tokens else address.strip()

    @staticmethod
    def _house_number_matches(query: str, result: dict) -> bool:
        """Whether the match carries a house number the query asked for.

        A CONTENT check, deliberately independent of `_is_dominant`'s
        relative score comparison — see `_HOUSE_NUMBER_RE` for why score
        cannot do this job.

        Returns True when the query names no number at all: "דיזנגוף תל
        אביב" is a legitimate street-level query, and a street-level match
        is the honest answer to it (reported as `approximate` quality).

        Any overlap counts, rather than requiring every number to appear: a
        planner-extracted address can carry incidental digits, and demanding
        all of them would reject real matches.
        """
        wanted = set(_HOUSE_NUMBER_RE.findall(query))
        if not wanted:
            return True
        found = set(_HOUSE_NUMBER_RE.findall(str(result.get("text") or "")))
        return bool(wanted & found)

    async def _street_level_match(
        self, client: GovmapClient, query: str, verify_against: str
    ) -> dict | None:
        """The street a numbered query names, when the number is not indexed.

        Re-runs the SAME query with the house number removed and returns the
        matched street, or `None` when nothing survives the checks below —
        in which case the caller reports `not_found`, exactly as before.

        Costs one extra HTTP call, and only ever on this failure path.

        THE CHECKS ARE THE WHOLE POINT. `_house_number_matches` exists
        because govmap's fuzzy search lands nonsense on real streets, and a
        fallback that simply accepted the street would undo it. So:

          1. `type == "street"` only. A settlement or neighbourhood match
             means the street was never found — "רחוב שלא נמצא 777 חיפה"
             matches a settlement literally named "רחוב".
          2. Still dominant among the streets returned, so an address that
             is genuinely ambiguous between two streets stays unresolved.
          3. The name must actually match (`_street_name_matches`), which
             is what keeps "כתובת לא קיימת 999" from resolving to
             "הקרן קיימת".
        """
        for street_query in self._street_queries(query):  # noqa: PLR1702 - flat enough
            results = await client.autocomplete(street_query)
            streets = [
                r
                for r in results
                if isinstance(r, dict) and str(r.get("type", "")).lower() == "street"
            ]
            if not streets or not self._is_dominant(streets):
                continue

            best = streets[0]
            if not self._street_name_matches(verify_against, str(best.get("text") or "")):
                logger.info(
                    "street-level fallback rejected: %r does not name the same street as %r",
                    best.get("text"),
                    query,
                )
                continue
            return best
        return None

    @staticmethod
    def _street_queries(query: str) -> list[str]:
        """The form of `query` that asks about the street alone: the same
        text with the house number removed.

        Just the one form. Stripping the Hebrew preposition is handled a
        level up, by `execute`, which re-runs the WHOLE search on the
        stripped wording - so this helper never has to know about it, and a
        query that needs both corrections gets them without either one
        being applied twice.
        """
        tokens = [t for t in query.split() if not _DIGIT_ONLY_TOKEN_RE.match(t)]
        if not tokens:
            return []
        # Nothing was actually removed: the fallback would just repeat the
        # query that already failed.
        stripped = " ".join(tokens)
        if stripped == query:
            return []
        return [stripped]

    @staticmethod
    def _name_tokens(text: str) -> list[str]:
        """Words worth comparing in an address, punctuation and digits gone.

        Punctuation is flattened rather than split on, so "תל-אביב" and
        "תל אביב" — govmap returns both spellings for the same city —
        tokenize identically.
        """
        cleaned = re.sub(r"[^\w\s]", " ", text)
        return [
            token
            for token in cleaned.split()
            if len(token) >= _MIN_COMPARABLE_TOKEN_LEN and not token.isdigit()
        ]

    @staticmethod
    def _token_variants(token: str) -> set[str]:
        """A word and, if it carries one, the same word without its prefix.

        Comparison only — see `_HEBREW_ONE_LETTER_PREFIXES`.
        """
        variants = {token}
        if len(token) >= _MIN_PREFIXED_TOKEN_LEN and token[0] in _HEBREW_ONE_LETTER_PREFIXES:
            variants.add(token[1:])
        return variants

    @classmethod
    def _street_name_matches(cls, query: str, matched_text: str) -> bool:
        """Whether `matched_text` names the same place the query asked for.

        EVERY word of the query must appear in the match. That is
        deliberately strict, because this is the only thing standing
        between the street-level fallback and the junk matches
        `_HOUSE_NUMBER_RE` was written to reject:

            "בשמואל תמיר 2 תל אביב" vs "שמואל תמיר תל אביב"  -> True
                (every word present; "בשמואל" matches via its prefix)
            "כתובת לא קיימת 999 תל אביב" vs "הקרן קיימת תל אביב" -> False
                (only "קיימת" is shared — the fuzzy hit that started all
                 of this)

        The cost of being strict is that an address padded with words that
        are not part of its name ("קומה 3") loses the fallback and reports
        `not_found`, which is the safe direction to fail in.
        """
        matched: set[str] = set()
        for token in cls._name_tokens(matched_text):
            matched |= cls._token_variants(token)
        if not matched:
            return False
        return all(
            cls._token_variants(token) & matched for token in cls._name_tokens(query)
        )

    @staticmethod
    def _is_dominant(results: list[dict]) -> bool:
        """Whether the top result is clearly the single right answer.

        A single result is dominant by definition. Otherwise the best score
        must beat the runner-up by `_DOMINANT_SCORE_RATIO`.

        The score gap, not the result count, is what matters: govmap
        routinely returns four matches where the first is 9x the second
        (a clear answer plus noise), and equally returns six matches with
        *identical* scores in six different cities (no answer at all).
        Counting results would misclassify both.
        """
        if len(results) == 1:
            return True
        try:
            best = float(results[0].get("score", 0))
            runner_up = float(results[1].get("score", 0))
        except (TypeError, ValueError):
            return False
        if runner_up <= 0:
            return best > 0
        return best / runner_up >= _DOMINANT_SCORE_RATIO

    async def _lookup_neighborhood(
        self, client: GovmapClient, point: tuple[float, float]
    ) -> str | None:
        """Find the neighbourhood name for a resolved point.

        Costs two extra HTTP calls, and is deliberately best-effort: a
        failure here downgrades the answer (no neighbourhood) but must not
        fail the whole resolution, because the coordinates are already
        correct and useful without it.

        WHY THE ROUNDABOUT ROUTE: govmap has no "neighbourhood at this
        point" endpoint. Its `layers-catalog/entitiesByPoint` returns HTTP
        400 for every payload shape documented by the public reference
        clients. What does work is the real-estate data, where every deal
        carries a `neighborhood` field — so we find one polygon near the
        point and read the neighbourhood off its transactions.

        This matters because `price_signal` keys its comparables by
        neighbourhood, and the value must be the same Hebrew name the deal
        data uses (e.g. "לב העיר") for the two tools to line up.
        """
        try:
            polygon_ids = await client.polygons_by_radius(*point)
            if not polygon_ids:
                return None
            # Any single polygon answers this: every polygon belongs to
            # exactly one neighbourhood, and all deals it returns carry the
            # same name. Verified across 10 polygons per address.
            deals = await client.neighborhood_deals(polygon_ids[0])
            for deal in deals:
                if deal.neighborhood:
                    return deal.neighborhood.strip()
            return None
        except Exception:
            logger.warning("Neighbourhood lookup failed; returning without it", exc_info=True)
            return None

    # --- result builders ---------------------------------------------------

    def _resolved(
        self,
        best: dict,
        lat: float,
        lon: float,
        neighborhood: str | None,
        extra_limitation: str | None = None,
    ) -> ToolResult[GeoResolverOutput]:
        """One confident match.

        `extra_limitation` carries anything the caller learned on the way to
        this match that the match itself cannot express — currently only the
        street-level fallback's missing house number.
        """
        result_type = str(best.get("type", "")).lower()
        quality = _QUALITY_BY_TYPE.get(result_type, "low_confidence")

        limitations = [_STRAIGHT_LINE_NOTE]
        if extra_limitation:
            limitations.append(extra_limitation)
        if quality != "exact":
            limitations.append(
                f"Matched at '{result_type or 'unknown'}' level rather than a specific "
                "building, so the position is approximate."
            )
        if neighborhood is None:
            limitations.append(
                "The neighbourhood for this address could not be determined, which may "
                "limit downstream price comparison."
            )

        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="resolved",
                # govmap's canonical spelling, not the caller's raw input.
                normalized_address=str(best.get("text", "")).strip() or None,
                lat=lat,
                lon=lon,
                resolution_quality=quality,
                neighborhood=neighborhood,
                source=_SOURCE,
                limitations=limitations,
            ),
        )

    def _ambiguous(self, results: list[dict]) -> ToolResult[GeoResolverOutput]:
        """Several plausible matches, none dominant.

        Per the contract, `lat`/`lon`/`normalized_address` stay unset —
        there is no single answer to act on, and picking one arbitrarily
        would hand downstream tools a confident-looking wrong location.
        """
        # Deduplicated, because govmap routinely returns the same place
        # name several times (different underlying records). Listing
        # "999, 999" as two distinct options helps nobody choose.
        seen: set[str] = set()
        candidates: list[str] = []
        for result in results:
            text = str(result.get("text", "")).strip()
            if text and text not in seen:
                seen.add(text)
                candidates.append(text)
            if len(candidates) >= _MAX_CANDIDATES:
                break
        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="ambiguous",
                candidates=candidates,
                source=_SOURCE,
                limitations=[
                    f"{len(candidates)} comparable matches were found and none was clearly "
                    "the intended one; a city or house number would disambiguate.",
                ],
            ),
        )

    @staticmethod
    def _not_found(address: str) -> ToolResult[GeoResolverOutput]:
        """No match at all — still a successful run, not an error."""
        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="not_found",
                source=_SOURCE,
                limitations=[
                    f"No Israeli address matching '{address}' was found in the government "
                    "address index.",
                ],
            ),
        )
