"""Candidate fuel stations along a route: one bounding-box query + corridor projection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .geo import MILES_PER_DEG_LAT, RouteIndex
from .models import FuelStation
from .optimizer import Candidate
from .routing import Route


@dataclass(frozen=True)
class Selection:
    candidates: list[Candidate]
    off_route_miles: dict[int, float]  # station pk -> miles from the route
    considered: int  # rows returned by the bounding-box query


def select_candidates(route: Route, corridor_miles: float) -> Selection:
    lats = [p[0] for p in route.points]
    lngs = [p[1] for p in route.points]
    dlat = corridor_miles / MILES_PER_DEG_LAT
    worst_lat = max(abs(min(lats)), abs(max(lats)))
    dlng = corridor_miles / (MILES_PER_DEG_LAT * math.cos(math.radians(worst_lat)))
    rows = FuelStation.objects.filter(
        latitude__range=(min(lats) - dlat, max(lats) + dlat),
        longitude__range=(min(lngs) - dlng, max(lngs) + dlng),
    ).values_list("id", "latitude", "longitude", "price")

    index = RouteIndex(route.points, corridor_miles, total_miles=route.distance_miles)
    candidates: list[Candidate] = []
    off: dict[int, float] = {}
    considered = 0
    for pk, lat, lng, price in rows:
        considered += 1
        hit = index.locate(float(lat), float(lng))
        if hit is None:
            continue
        along, off_miles = hit
        candidates.append(Candidate(pk, along, Decimal(price)))
        off[pk] = off_miles
    return Selection(candidates, off, considered)
