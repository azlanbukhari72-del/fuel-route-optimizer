"""Pure geometry helpers. Convention: every point is (lat, lng). No Django, no I/O."""

from __future__ import annotations

import math

EARTH_RADIUS_MILES = 3958.7613
MILES_PER_DEG_LAT = EARTH_RADIUS_MILES * math.pi / 180  # ~69.09


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google/ORS encoded polyline into (lat, lng) tuples."""
    factor = 10**precision
    points: list[tuple[float, float]] = []
    index = lat = lng = 0
    n = len(encoded)
    while index < n:
        pair = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= n:
                    raise ValueError("truncated polyline")
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            pair.append(~(result >> 1) if result & 1 else result >> 1)
        lat += pair[0]
        lng += pair[1]
        points.append((lat / factor, lng / factor))
    return points


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    out: list[str] = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        for delta in (ilat - prev_lat, ilng - prev_lng):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)


def decimate(
    points: list[tuple[float, float]], min_spacing_miles: float
) -> list[tuple[float, float]]:
    """O(n) thinning: keep a vertex only when it is >= min_spacing from the last kept one."""
    if len(points) <= 2:
        return list(points)
    kept = [points[0]]
    for p in points[1:-1]:
        if haversine_miles(kept[-1], p) >= min_spacing_miles:
            kept.append(p)
    kept.append(points[-1])
    return kept


def simplify(
    points: list[tuple[float, float]], tolerance_miles: float
) -> list[tuple[float, float]]:
    """Iterative Ramer-Douglas-Peucker. Keeps endpoints; max deviation <= tolerance."""
    n = len(points)
    if n <= 2:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        far, far_d = -1, tolerance_miles
        a, b = points[lo], points[hi]
        for i in range(lo + 1, hi):
            d = _point_segment(points[i], a, b)[0]
            if d > far_d:
                far, far_d = i, d
        if far >= 0:
            keep[far] = True
            stack.append((lo, far))
            stack.append((far, hi))
    return [p for p, k in zip(points, keep, strict=True) if k]


def _point_segment(p, a, b) -> tuple[float, float]:
    """Distance (miles) from p to segment a-b and the projection fraction t in [0, 1].

    Local equirectangular approximation around p's latitude: accurate to well under a percent
    at the tens-of-miles scale this is used for.
    """
    k = math.cos(math.radians(p[0])) * MILES_PER_DEG_LAT
    ax, ay = (a[1] - p[1]) * k, (a[0] - p[0]) * MILES_PER_DEG_LAT
    bx, by = (b[1] - p[1]) * k, (b[0] - p[0]) * MILES_PER_DEG_LAT
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy), t


class RouteIndex:
    """Locate points against a route: distance to it and mileage along it.

    A uniform grid maps cell -> ids of segments passing within ``corridor_miles`` of that cell.
    Each segment is registered in EVERY cell its corridor touches (sampled along the whole
    segment, not just at its vertices), so a station beside the middle of a long straight
    segment is still found. Lookup is then a single dict access plus a handful of exact
    point-to-segment distance tests.
    """

    CELL_DEG = 0.1

    def __init__(
        self,
        points: list[tuple[float, float]],
        corridor_miles: float,
        total_miles: float | None = None,
    ):
        if len(points) < 2:
            raise ValueError("route needs at least two points")
        self.points = points
        self.corridor = corridor_miles
        self.cum = [0.0]
        for a, b in zip(points, points[1:], strict=False):
            self.cum.append(self.cum[-1] + haversine_miles(a, b))
        geometric = self.cum[-1]
        # Provider distance is authoritative; stretch our path length to match it.
        self.scale = (total_miles / geometric) if total_miles and geometric > 0 else 1.0
        self.total_miles = geometric * self.scale
        self.grid: dict[tuple[int, int], list[int]] = {}
        self._build()

    def _build(self) -> None:
        cell = self.CELL_DEG
        step = cell  # sample spacing in degrees along a segment (inf-norm)
        pad = step / 2
        dlat_c = self.corridor / MILES_PER_DEG_LAT
        for i in range(len(self.points) - 1):
            (la, ga), (lb, gb) = self.points[i], self.points[i + 1]
            n = max(1, math.ceil(max(abs(lb - la), abs(gb - ga)) / step))
            seen: set[tuple[int, int]] = set()
            for s in range(n + 1):
                t = s / n
                lat, lng = la + (lb - la) * t, ga + (gb - ga) * t
                dlat = dlat_c + pad
                dlng = (
                    self.corridor / (MILES_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.2))
                    + pad
                )
                for cy in range(
                    math.floor((lat - dlat) / cell), math.floor((lat + dlat) / cell) + 1
                ):
                    for cx in range(
                        math.floor((lng - dlng) / cell), math.floor((lng + dlng) / cell) + 1
                    ):
                        if (cy, cx) not in seen:
                            seen.add((cy, cx))
                            self.grid.setdefault((cy, cx), []).append(i)

    def locate(self, lat: float, lng: float) -> tuple[float, float] | None:
        """Return (miles along route, miles off route) or None if outside the corridor."""
        segs = self.grid.get((math.floor(lat / self.CELL_DEG), math.floor(lng / self.CELL_DEG)))
        if not segs:
            return None
        best = None
        for i in segs:
            d, t = _point_segment((lat, lng), self.points[i], self.points[i + 1])
            if best is None or d < best[0]:
                best = (d, i, t)
        d, i, t = best
        if d > self.corridor:
            return None
        along = self.cum[i] + t * (self.cum[i + 1] - self.cum[i])
        return along * self.scale, d
