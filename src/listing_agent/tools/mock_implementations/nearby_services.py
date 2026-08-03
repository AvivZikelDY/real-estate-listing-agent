"""Deterministic mock `nearby_services` implementation.

Uses a small hardcoded dataset of services near the known mock address
("HaYarkon 120, Tel Aviv"). Distances are computed straight-line
(haversine) — never claimed to be walking distance or walking time.
"""

import math

from src.listing_agent.tools.schemas import (
    NearbyPlace,
    NearbyServicesInput,
    NearbyServicesOutput,
    ServiceCategorySummary,
    ToolResult,
)

_EARTH_RADIUS_M = 6_371_000.0
_SOURCE = "mock_dataset"

_STRAIGHT_LINE_LIMITATION = (
    "Distances are straight-line (as-the-crow-flies) estimates, not walking "
    "distances or walking times."
)

# A small hardcoded dataset of services near HaYarkon 120, Tel Aviv.
_SERVICES = [
    {"category": "supermarket", "name": "Tiv Taam HaYarkon", "lat": 32.0855, "lon": 34.7742},
    {"category": "school", "name": "Gymnasia Herzliya", "lat": 32.0802, "lon": 34.7751},
    {"category": "park", "name": "Independence Park", "lat": 32.0870, "lon": 34.7721},
    {"category": "pharmacy", "name": "Super-Pharm HaYarkon", "lat": 32.0851, "lon": 34.7745},
]


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line (haversine) distance in meters between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c


class MockNearbyServices:
    """Deterministic nearby_services mock backed by a small local dataset."""

    name = "nearby_services"

    async def execute(self, input_model: NearbyServicesInput) -> ToolResult[NearbyServicesOutput]:
        requested_categories = set(input_model.categories)
        places: list[NearbyPlace] = []
        by_category: dict[str, list[float]] = {}

        for service in _SERVICES:
            if service["category"] not in requested_categories:
                continue
            distance = _distance_m(input_model.lat, input_model.lon, service["lat"], service["lon"])
            if distance > input_model.radius_m:
                continue
            by_category.setdefault(service["category"], []).append(distance)
            places.append(
                NearbyPlace(
                    name=service["name"],
                    category=service["category"],
                    distance_m=round(distance, 1),
                    source=_SOURCE,
                )
            )

        places.sort(key=lambda place: place.distance_m)
        categories = [
            ServiceCategorySummary(
                category=category,
                count=len(distances),
                nearest_distance_m=round(min(distances), 1),
            )
            for category, distances in sorted(by_category.items())
        ]

        limitations = [_STRAIGHT_LINE_LIMITATION]
        if not categories:
            evidence_quality = "low"
            limitations.append(
                "No known services found within the mock dataset for this location/radius."
            )
        else:
            nearest_overall = min(cat.nearest_distance_m for cat in categories)
            evidence_quality = "high" if nearest_overall <= 300 else "medium"
            limitations.append(
                "Based on a small hardcoded mock dataset, not a live services database."
            )

        return ToolResult(
            status="ok",
            data=NearbyServicesOutput(
                places=places,
                categories=categories,
                evidence_quality=evidence_quality,
                limitations=limitations,
            ),
        )
