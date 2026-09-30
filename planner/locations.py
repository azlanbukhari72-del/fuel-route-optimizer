"""Turn request locations ("City, ST" or {lat, lng}) into coordinates. Zero external calls."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db.models import Q

from .errors import LocationNotFound, LocationOutsideServiceArea
from .models import Place
from .names import lookup_keys

# Coarse contiguous-US rectangle (NOT a border polygon): see README.
LAT_RANGE = (24.4, 49.6)
LNG_RANGE = (-125.0, -66.9)
_CITY_ST = re.compile(r"^\s*(?P<city>.+?)\s*,\s*(?P<state>[A-Za-z]{2})\s*$")


@dataclass(frozen=True)
class Location:
    label: str
    lat: float
    lng: float

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lng)


def _check_area(lat: float, lng: float, label: str) -> None:
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
        raise LocationOutsideServiceArea(details={"location": label})


def resolve_locations(*raw: str | dict) -> list[Location]:
    """Resolve any number of inputs with at most ONE database query (for all text inputs)."""
    parsed: list[tuple[str, str] | None] = []
    query = Q()
    for item in raw:
        if isinstance(item, str):
            m = _CITY_ST.match(item)
            if not m:
                raise LocationNotFound(
                    "Use 'City, ST' (e.g. 'Chicago, IL') or {lat, lng}.", {"location": item}
                )
            city, state = m["city"], m["state"].upper()
            parsed.append((city, state))
            query |= Q(state=state, name_key__in=lookup_keys(city))
        else:
            parsed.append(None)

    found: dict[tuple[str, str], Place] = {}
    if query:
        found = {(p.state, p.name_key): p for p in Place.objects.filter(query)}

    out: list[Location] = []
    for item, p in zip(raw, parsed, strict=True):
        if p is None:
            lat, lng = float(item["lat"]), float(item["lng"])
            _check_area(lat, lng, f"{lat},{lng}")
            out.append(Location(f"{lat:.4f},{lng:.4f}", lat, lng))
            continue
        city, state = p
        place = next((found[(state, k)] for k in lookup_keys(city) if (state, k) in found), None)
        if place is None:
            raise LocationNotFound(details={"location": f"{city}, {state}"})
        lat, lng = float(place.latitude), float(place.longitude)
        _check_area(lat, lng, str(place))
        out.append(Location(f"{place.name}, {place.state}", lat, lng))
    return out
