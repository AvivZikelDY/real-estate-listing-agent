"""Tests for `OverpassClient._post` — the mirror-fallback layer.

The `NirNearbyServices` tests stub the whole `OverpassClient`, so the mirror
loop itself was previously untested. These tests drive it through
`httpx.MockTransport` (an httpx test double that answers requests from a
local handler function), so nothing here touches the network.

`client._http` is assigned directly rather than via `async with`: the
context manager builds a real `httpx.AsyncClient` with no way to inject a
transport, and `_post` needs nothing else from it. Keeping the injection in
the test avoids adding a test-only parameter to the production API.
"""

import httpx
import pytest

from src.listing_agent.agent.tool_executor.errors import (
    ToolProviderUnavailableError,
    ToolRateLimitedError,
)
from src.listing_agent.tools.nir_implementations.overpass_client import OverpassClient

_MIRROR_A = "https://mirror-a.test/api/interpreter"
_MIRROR_B = "https://mirror-b.test/api/interpreter"

# One supermarket, in the shape Overpass actually returns.
_ONE_PLACE = {
    "elements": [
        {
            "type": "node",
            "lat": 32.0632,
            "lon": 34.7726,
            "tags": {"shop": "supermarket", "name": "סופר"},
        }
    ]
}

# What an overloaded Overpass sends: HTTP 200, no places, and its own
# internal failure described in `remark`.
_TIMED_OUT_BODY = {
    "elements": [],
    "remark": 'runtime error: Query timed out in "query" at line 3',
}


def _client(handler, mirrors: tuple[str, ...] = (_MIRROR_A, _MIRROR_B)) -> OverpassClient:
    """An `OverpassClient` whose HTTP calls are served by `handler`."""
    client = OverpassClient(mirrors=mirrors)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# --- the remark false negative --------------------------------------------


@pytest.mark.asyncio
async def test_remark_in_a_200_response_falls_through_to_the_next_mirror() -> None:
    """A 200 OK carrying `remark` must not be accepted as an empty area.

    This is the false negative that reached the buyer's report as fact:
    trusting the status code alone turned an overloaded mirror into a
    confident "no supermarket within 500m".
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == _MIRROR_A:
            return httpx.Response(200, json=_TIMED_OUT_BODY)
        return httpx.Response(200, json=_ONE_PLACE)

    places = await _client(handler).find_places(32.0632, 34.7726, 500, ["supermarket"])

    assert seen == [_MIRROR_A, _MIRROR_B], "the remark mirror should have been abandoned"
    assert len(places) == 1
    assert places[0].name == "סופר"


@pytest.mark.asyncio
async def test_remark_from_every_mirror_raises_instead_of_reporting_nothing_found() -> None:
    """When no mirror answers cleanly, this must fail loudly.

    Reporting zero places here is the exact bug: `nearby_services` would
    return status="ok" with an empty list, indistinguishable from a real
    "nothing nearby" answer.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TIMED_OUT_BODY)

    with pytest.raises(ToolProviderUnavailableError, match="Query timed out"):
        await _client(handler).find_places(32.0632, 34.7726, 500, ["supermarket"])


@pytest.mark.asyncio
async def test_a_genuinely_empty_area_is_still_an_empty_list_not_an_error() -> None:
    """No remark and no elements is a real business answer: nothing here.

    The remark check must not turn a truthful "we looked and found nothing"
    into a technical failure.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elements": []})

    places = await _client(handler).find_places(32.0632, 34.7726, 500, ["supermarket"])

    assert places == []


# --- the pre-existing status-code paths, previously untested ---------------


@pytest.mark.asyncio
async def test_504_falls_through_to_the_next_mirror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _MIRROR_A:
            return httpx.Response(504)
        return httpx.Response(200, json=_ONE_PLACE)

    places = await _client(handler).find_places(32.0632, 34.7726, 500, ["supermarket"])

    assert len(places) == 1


@pytest.mark.asyncio
async def test_rate_limit_from_every_mirror_reports_the_last_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(ToolRateLimitedError):
        await _client(handler).find_places(32.0632, 34.7726, 500, ["supermarket"])


@pytest.mark.asyncio
async def test_non_json_body_falls_through_to_the_next_mirror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _MIRROR_A:
            return httpx.Response(200, text="<html>502 Bad Gateway</html>")
        return httpx.Response(200, json=_ONE_PLACE)

    places = await _client(handler).find_places(32.0632, 34.7726, 500, ["supermarket"])

    assert len(places) == 1
