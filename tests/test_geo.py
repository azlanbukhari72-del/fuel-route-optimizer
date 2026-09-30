import json
import math
import random
from pathlib import Path

import pytest

from planner.geo import (
    MILES_PER_DEG_LAT,
    RouteIndex,
    decimate,
    decode_polyline,
    encode_polyline,
    haversine_miles,
    simplify,
)


def test_haversine_known_distance():
    nyc, la = (40.7128, -74.0060), (34.0522, -118.2437)
    assert haversine_miles(nyc, la) == pytest.approx(2445, abs=10)
    assert haversine_miles(nyc, nyc) == 0


def test_polyline_roundtrip_and_known_vector():
    # Reference vector from Google's polyline documentation
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]
    pts = [(40.71283, -74.00602), (34.05221, -118.24369), (0.0, 0.0), (-12.5, 130.25)]
    assert decode_polyline(encode_polyline(pts)) == pytest.approx(pts)


def test_decode_truncated_polyline_raises():
    with pytest.raises(ValueError):
        decode_polyline("_p~iF~ps|")


def test_decimate_keeps_endpoints_and_spacing():
    pts = [(40 + i * 0.001, -100.0) for i in range(2000)]  # ~0.069 mi apart
    out = decimate(pts, 0.5)
    assert out[0] == pts[0] and out[-1] == pts[-1]
    assert len(out) < len(pts) / 5


def test_simplify_bounds_error_and_keeps_endpoints():
    rng = random.Random(3)
    pts = [(40 + i * 0.01, -100 + math.sin(i / 7) * 0.05 + rng.random() * 1e-4) for i in range(300)]
    out = simplify(pts, 0.25)
    assert out[0] == pts[0] and out[-1] == pts[-1] and len(out) < len(pts)
    ix = RouteIndex(out, corridor_miles=1.0)
    for p in pts:  # every original point is within tolerance (+ approximation slack) of result
        loc = ix.locate(*p)
        assert loc is not None and loc[1] <= 0.3


# --- RouteIndex -------------------------------------------------------------------------------


def east_west_route():
    # two-vertex segment ~100 miles long along lat 40 (many grid cells wide)
    lng_span = 100 / (MILES_PER_DEG_LAT * math.cos(math.radians(40)))
    return [(40.0, -100.0), (40.0, -100.0 + lng_span)], lng_span


def test_locate_on_route_and_position():
    pts, span = east_west_route()
    ix = RouteIndex(pts, corridor_miles=10)
    along, off = ix.locate(40.0, -100.0 + span / 2)
    assert along == pytest.approx(ix.total_miles / 2, rel=1e-3) and off < 0.01


def test_long_segment_midpoint_station_found():
    # Fails if only cells containing the two vertices were indexed.
    pts, span = east_west_route()
    ix = RouteIndex(pts, corridor_miles=10)
    lat_off = 3 / MILES_PER_DEG_LAT
    found = ix.locate(40.0 + lat_off, -100.0 + span / 2)
    assert found is not None and found[1] == pytest.approx(3, abs=0.05)


def test_corridor_boundary():
    pts, span = east_west_route()
    ix = RouteIndex(pts, corridor_miles=10)
    mid = -100.0 + span / 2
    assert ix.locate(40.0 + 9.9 / MILES_PER_DEG_LAT, mid) is not None
    assert ix.locate(40.0 + 10.1 / MILES_PER_DEG_LAT, mid) is None


def test_before_start_and_after_end_clamp_to_ends():
    pts, span = east_west_route()
    ix = RouteIndex(pts, corridor_miles=10)
    before = ix.locate(40.0, -100.0 - 5 / (MILES_PER_DEG_LAT * math.cos(math.radians(40))))
    assert before is not None and before[0] == pytest.approx(0, abs=0.01)
    assert ix.locate(40.0, -100.0 - 30 / 53) is None  # ~30 mi before the start: outside


def test_scale_to_provider_distance():
    pts, _ = east_west_route()
    ix = RouteIndex(pts, corridor_miles=10, total_miles=120.0)
    assert ix.total_miles == pytest.approx(120.0)
    along, _ = ix.locate(*pts[1])
    assert along == pytest.approx(120.0, rel=1e-6)


def test_position_monotonic_along_route():
    pts = [(40 + i * 0.05, -100 + i * 0.05) for i in range(40)]
    ix = RouteIndex(pts, corridor_miles=10)
    last = -1.0
    for lat, lng in pts:
        along, _ = ix.locate(lat, lng)
        assert along > last
        last = along


def brute_force(ix, lat, lng):
    from planner.geo import _point_segment

    best = min(
        (
            _point_segment((lat, lng), ix.points[i], ix.points[i + 1]) + (i,)
            for i in range(len(ix.points) - 1)
        ),
        key=lambda x: x[0],
    )
    d, t, i = best
    return (ix.cum[i] + t * (ix.cum[i + 1] - ix.cum[i])) * ix.scale, d


def test_grid_matches_brute_force_on_random_routes():
    rng = random.Random(11)
    checked_in = checked_out = 0
    for _ in range(15):
        lat, lng = rng.uniform(30, 45), rng.uniform(-110, -80)
        pts = [(lat, lng)]
        for _ in range(rng.randint(2, 25)):
            lat += rng.uniform(-0.6, 0.6)
            lng += rng.uniform(0.0, 1.5)  # includes long segments spanning many cells
            pts.append((lat, lng))
        ix = RouteIndex(pts, corridor_miles=10)
        for _ in range(150):
            p = (
                rng.uniform(min(x[0] for x in pts) - 0.4, max(x[0] for x in pts) + 0.4),
                rng.uniform(pts[0][1] - 0.4, pts[-1][1] + 0.4),
            )
            expected = brute_force(ix, *p)
            got = ix.locate(*p)
            if expected[1] <= 10 - 1e-6:
                assert got is not None, p
                assert got[1] == pytest.approx(expected[1], abs=1e-6)
                assert got[0] == pytest.approx(expected[0], abs=1e-6)
                checked_in += 1
            elif expected[1] > 10 + 1e-6:
                assert got is None
                checked_out += 1
    assert checked_in > 100 and checked_out > 100


@pytest.mark.skipif(
    not Path(__file__).with_name("fixtures").joinpath("ors_sample.json").exists(),
    reason="fixture not present",
)
def test_real_ors_geometry_decodes():
    data = json.loads(Path(__file__).with_name("fixtures").joinpath("ors_sample.json").read_text())
    pts = decode_polyline(data["routes"][0]["geometry"])
    assert pts[0] == pytest.approx((40.71, -74.0), abs=0.05)
