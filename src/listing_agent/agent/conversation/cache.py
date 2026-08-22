"""Cache-aware evidence collection for stateful follow-up questions.

Sits directly above `evidence_collector.collect_evidence`: reduces a
validated plan's tool calls down to only the work not already answered by
an earlier turn in the same conversation, executes whatever remains
through the normal `ToolExecutor` seam (so `LocalToolExecutor` and a
future `McpToolExecutor` never need to know reuse exists), and merges
cached + freshly executed results back into one ordered `ToolEvidence`
list - the same shape `collect_evidence` alone would have produced for the
full (unreduced) plan.

Claim re-linking for reused results goes through the same
`evidence_mapper.map_tool_evidence` every fresh result goes through - a
cached `ToolExecutionResult` is simply mapped again against the *current*
turn's claims. No separate relinking/requirement/selector framework.

Reduction only ever applies to `nearby_services`/`price_signal` when
`geo_resolver`'s output is already known *before* any tool executes this
turn - i.e. only when `geo_resolver` itself was reused from the cache.
When `geo_resolver` has to run fresh this turn (e.g. the very first turn
of a conversation), nothing downstream could have been cached for this
address yet either, so those tools simply run in full, exactly as
`collect_evidence` already does on its own.
"""

from src.listing_agent.agent.conversation.context import (
    CachedNearbyCategory,
    ConversationContext,
    geo_cache_key,
    nearby_cache_key,
    price_cache_key,
)
from src.listing_agent.agent.evidence_collector import collect_evidence
from src.listing_agent.agent.evidence_mapper import map_tool_evidence
from src.listing_agent.agent.schemas import ToolCall, ToolEvidence, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import ToolExecutor
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult
from src.listing_agent.agent.tool_input_builder import MissingDependencyError, build_tool_input
from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyPlace,
    NearbyServicesInput,
    NearbyServicesOutput,
    PriceSignalInput,
    PriceSignalOutput,
    ServiceCategorySummary,
)

_EVIDENCE_QUALITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _combine_evidence_quality(qualities: list[str]) -> str:
    """The conservative (worst) evidence quality across combined parts."""
    if not qualities:
        return "low"
    return min(qualities, key=lambda quality: _EVIDENCE_QUALITY_RANK[quality])


def _combined_nearby_output(
    cached_parts: dict[str, CachedNearbyCategory],
    fresh_output: NearbyServicesOutput | None,
) -> NearbyServicesOutput:
    """Combine previously cached per-category results with a freshly
    executed result (for the categories that were missing) into one
    `NearbyServicesOutput`, as if a single call had produced it all."""
    places: list[NearbyPlace] = []
    categories: list[ServiceCategorySummary] = []
    qualities: list[str] = []
    limitations: list[str] = []
    seen_limitations: set[str] = set()

    def _add_limitations(items: list[str]) -> None:
        for item in items:
            if item not in seen_limitations:
                seen_limitations.add(item)
                limitations.append(item)

    for cached in cached_parts.values():
        places.extend(cached.places)
        if cached.summary is not None:
            categories.append(cached.summary)
        qualities.append(cached.evidence_quality)
        _add_limitations(cached.limitations)

    if fresh_output is not None:
        places.extend(fresh_output.places)
        categories.extend(fresh_output.categories)
        qualities.append(fresh_output.evidence_quality)
        _add_limitations(list(fresh_output.limitations))

    return NearbyServicesOutput(
        places=places,
        categories=categories,
        evidence_quality=_combine_evidence_quality(qualities),
        limitations=limitations,
    )


async def collect_evidence_with_context(
    plan: TriagePlan,
    tool_executor: ToolExecutor,
    tracer: StepTracer,
    context: ConversationContext,
    *,
    max_total_tool_execution_seconds: float,
) -> tuple[list[ToolEvidence], list[str], dict[str, object]]:
    """Like `evidence_collector.collect_evidence`, but reuses `context`'s
    already-cached tool results where possible.

    Returns `(tool_results, executed_tools, trace_detail)`. `tool_results`
    and `executed_tools` have the same meaning/shape as
    `collect_evidence`'s return value. `trace_detail` is the `Phase 13`
    `context_reuse` step payload: `{"reused": [...], "executed": [...],
    "tool_calls_saved": N}`.
    """
    geo_call = next((call for call in plan.tool_calls if call.tool == "geo_resolver"), None)
    nearby_call = next((call for call in plan.tool_calls if call.tool == "nearby_services"), None)
    price_call = next((call for call in plan.tool_calls if call.tool == "price_signal"), None)

    reused_evidence: dict[str, ToolEvidence] = {}
    reused_keys: list[str] = []
    geo_seed: GeoResolverOutput | None = None
    reduced_calls: list[ToolCall] = []

    # --- geo_resolver -----------------------------------------------------
    if geo_call is not None:
        geo_input = _try_build(geo_call, plan, None, GeoResolverInput)
        if geo_input is not None:
            cached_geo = context.cached_geo.get(geo_cache_key(geo_input))
            if cached_geo is not None:
                geo_seed = cached_geo
                reused_evidence["geo_resolver"] = map_tool_evidence(
                    geo_call,
                    ToolExecutionResult(status="ok", output=cached_geo),
                    plan.claims,
                )
                reused_keys.append("geo_resolver")
            else:
                reduced_calls.append(geo_call)
        else:
            # `build_tool_input` cannot even be attempted for a cache
            # lookup - most commonly the address names no city this agent
            # recognises (`MissingDependencyError`, the live geo_resolver's
            # no-city refusal).
            # No cache key can be computed, so there is nothing to reuse:
            # let the normal `collect_evidence` path run `geo_call` itself,
            # which raises/catches the same `MissingDependencyError` and
            # records the honest "skipped" evidence entry - never an
            # uncaught exception, never a cached failure.
            reduced_calls.append(geo_call)

    # --- nearby_services (only reducible once geo is already known) ------
    cached_nearby_parts: dict[str, CachedNearbyCategory] = {}
    executed_nearby_call: ToolCall | None = None
    nearby_fully_reused = False
    if nearby_call is not None:
        nearby_input = _try_build(nearby_call, plan, geo_seed, NearbyServicesInput)
        if geo_seed is not None and nearby_input is not None:
            missing_categories: list[str] = []
            for category in nearby_input.categories:
                key = nearby_cache_key(
                    nearby_input.lat, nearby_input.lon, nearby_input.radius_m, category
                )
                cached = context.cached_nearby.get(key)
                if cached is not None:
                    cached_nearby_parts[category] = cached
                else:
                    missing_categories.append(category)

            if not missing_categories:
                nearby_fully_reused = True
                reused_evidence["nearby_services"] = map_tool_evidence(
                    nearby_call,
                    ToolExecutionResult(
                        status="ok",
                        output=_combined_nearby_output(cached_nearby_parts, None),
                    ),
                    plan.claims,
                )
            else:
                if len(missing_categories) != len(nearby_input.categories):
                    executed_nearby_call = nearby_call.model_copy(
                        update={
                            "arguments": {**nearby_call.arguments, "categories": missing_categories}
                        }
                    )
                else:
                    executed_nearby_call = nearby_call
                reduced_calls.append(executed_nearby_call)
            reused_keys.extend(f"nearby:{category}" for category in cached_nearby_parts)
        else:
            executed_nearby_call = nearby_call
            reduced_calls.append(nearby_call)

    # --- price_signal -------------------------------------------------------
    price_fully_reused = False
    if price_call is not None:
        price_input = _try_build(price_call, plan, geo_seed, PriceSignalInput)
        if geo_seed is not None and price_input is not None:
            cached_price = context.cached_price.get(price_cache_key(price_input))
            if cached_price is not None:
                price_fully_reused = True
                reused_evidence["price_signal"] = map_tool_evidence(
                    price_call,
                    ToolExecutionResult(status="ok", output=cached_price),
                    plan.claims,
                )
                reused_keys.append("price_signal")
            else:
                reduced_calls.append(price_call)
        else:
            reduced_calls.append(price_call)

    reduced_plan = plan.model_copy(update={"tool_calls": reduced_calls})
    executed_results, executed_tools = await collect_evidence(
        reduced_plan,
        tool_executor,
        tracer,
        max_total_tool_execution_seconds=max_total_tool_execution_seconds,
        geo_output=geo_seed,
    )
    executed_by_tool: dict[str, ToolEvidence] = {
        evidence.tool: evidence for evidence in executed_results
    }

    # --- store newly-successful, cacheable results back into `context` ----
    geo_seed = _store_new_results(
        context,
        plan,
        geo_call=geo_call,
        executed_nearby_call=executed_nearby_call,
        price_call=None if price_fully_reused else price_call,
        executed_by_tool=executed_by_tool,
        geo_seed=geo_seed,
    )

    # --- merge partial nearby reuse (cached categories + freshly executed) -
    if (
        not nearby_fully_reused
        and executed_nearby_call is not None
        and cached_nearby_parts
        and "nearby_services" in executed_by_tool
    ):
        fresh = executed_by_tool["nearby_services"]
        if fresh.status == "ok" and fresh.output is not None:
            fresh_output = NearbyServicesOutput.model_validate(fresh.output)
            combined_output = _combined_nearby_output(cached_nearby_parts, fresh_output)
            executed_by_tool["nearby_services"] = map_tool_evidence(
                nearby_call,
                ToolExecutionResult(status="ok", output=combined_output),
                plan.claims,
            )

    combined: list[ToolEvidence] = []
    for call in plan.tool_calls:
        if call.tool in executed_by_tool:
            combined.append(executed_by_tool[call.tool])
        elif call.tool in reused_evidence:
            combined.append(reused_evidence[call.tool])

    trace_detail = {
        "reused": reused_keys,
        "executed": list(executed_tools),
        "tool_calls_saved": len(reused_keys),
    }
    return combined, executed_tools, trace_detail


def _try_build(
    call: ToolCall,
    plan: TriagePlan,
    geo_seed: GeoResolverOutput | None,
    expected_type: type,
):
    """Build `call`'s typed input, returning `None` instead of raising when
    a dependency (resolved geo) isn't available yet - used purely to probe
    whether cache-key computation is currently possible, never to decide
    whether the call itself should run (that remains `evidence_collector`'s
    job when the call is actually executed)."""
    try:
        built = build_tool_input(call, plan.property_details, geo_seed, plan.claims)
    except MissingDependencyError:
        return None
    return built if isinstance(built, expected_type) else None


def _store_new_results(
    context: ConversationContext,
    plan: TriagePlan,
    *,
    geo_call: ToolCall | None,
    executed_nearby_call: ToolCall | None,
    price_call: ToolCall | None,
    executed_by_tool: dict[str, ToolEvidence],
    geo_seed: GeoResolverOutput | None,
) -> GeoResolverOutput | None:
    """Store newly successful, cacheable results into `context` (Phase 11:
    only a resolved geo, any `ok` nearby result incl. empty, and a
    `compared`/`no_comparable_data` price result are ever cached).

    Returns the resolved geo output to use for building nearby/price cache
    keys (freshly resolved this turn, or the turn's existing seed).
    """
    if geo_call is not None and "geo_resolver" in executed_by_tool:
        evidence = executed_by_tool["geo_resolver"]
        is_resolved = evidence.output.get("outcome") == "resolved" if evidence.output else False
        if evidence.status == "ok" and is_resolved:
            resolved = GeoResolverOutput.model_validate(evidence.output)
            geo_seed = resolved
            # In practice `build_tool_input` cannot raise here: it already
            # succeeded once, deterministically (same `property_details`,
            # no side effects), for `collect_evidence` to have actually run
            # `geo_call` and produced this `"resolved"` evidence in the
            # first place. Guarded anyway, defensively: a `geo_seed` from a
            # genuine success must never be lost even if a future change
            # makes this re-build fail - only *caching* it for next-turn
            # reuse is skipped when the cache key can't be computed.
            geo_input = _try_build(geo_call, plan, None, GeoResolverInput)
            if geo_input is not None:
                context.cached_geo[geo_cache_key(geo_input)] = resolved
                context.resolved_geo = resolved

    nearby_ready = (
        executed_nearby_call is not None
        and "nearby_services" in executed_by_tool
        and geo_seed is not None
    )
    if nearby_ready:
        evidence = executed_by_tool["nearby_services"]
        if evidence.status == "ok" and evidence.output is not None:
            fresh_input = _try_build(executed_nearby_call, plan, geo_seed, NearbyServicesInput)
            if fresh_input is not None:
                fresh_output = NearbyServicesOutput.model_validate(evidence.output)
                # `fresh_output` may already be the *merged* (cached+fresh)
                # result if this is read after the merge step - callers
                # always invoke `_store_new_results` before merging, so at
                # this point it only ever reflects the categories actually
                # requested this call (`fresh_input.categories`).
                for category in fresh_input.categories:
                    places = [place for place in fresh_output.places if place.category == category]
                    summary = next(
                        (item for item in fresh_output.categories if item.category == category),
                        None,
                    )
                    key = nearby_cache_key(
                        fresh_input.lat, fresh_input.lon, fresh_input.radius_m, category
                    )
                    context.cached_nearby[key] = CachedNearbyCategory(
                        category=category,
                        places=places,
                        summary=summary,
                        evidence_quality=fresh_output.evidence_quality,
                        limitations=list(fresh_output.limitations),
                    )

    if price_call is not None and "price_signal" in executed_by_tool and geo_seed is not None:
        evidence = executed_by_tool["price_signal"]
        if evidence.status == "ok" and evidence.output is not None:
            output = PriceSignalOutput.model_validate(evidence.output)
            if output.outcome in ("compared", "no_comparable_data"):
                price_input = _try_build(price_call, plan, geo_seed, PriceSignalInput)
                if price_input is not None:
                    context.cached_price[price_cache_key(price_input)] = output

    return geo_seed


__all__ = ["collect_evidence_with_context"]
