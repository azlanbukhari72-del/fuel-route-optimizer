import json
import math
from decimal import Decimal as D
from pathlib import Path

import pytest
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from planner import pipeline
from planner.errors import (
    RouteNotFound,
    RoutingProviderError,
    RoutingRateLimited,
    RoutingTimeout,
)
from planner.geo import MILES_PER_DEG_LAT, decimate, decode_polyline
from planner.models import FuelStation, Place
from planner.routing import Route

URL = "/api/v1/routes/plan/"
LAT = 40.0
LNG0 = -100.0
DEG_PER_MILE = 1 / (MILES_PER_DEG_LAT * math.cos(math.radians(LAT)))


def lng_at(mile):
    return LNG0 + mile * DEG_PER_MILE


def straight_route(miles):
    pts = [(LAT, lng_at(m)) for m in range(0, int(miles) + 1, 20)]
    if pts[-1][1] != lng_at(miles):
        pts.append((LAT, lng_at(miles)))
    return Route(float(miles), miles * 1.2, pts)


def add_station(opis_id, mile, price, off_miles=0.0, lat=None):
    return FuelStation.objects.create(
        opis_id=opis_id,
        name=f"STOP {opis_id}",
        address="I-70, EXIT 1",
        city="Somewhere",
        state="KS",
        rack_id=1,
        price=D(str(price)),
        latitude=D(str(round(LAT + off_miles / MILES_PER_DEG_LAT if lat is None else lat, 6))),
        longitude=D(str(round(lng_at(mile), 6))),
        updated_at="2026-01-01T00:00:00Z",
    )


@pytest.fixture
def world(db):
    Place.objects.create(
        state="KS",
        name_key="start town",
        name="Start Town",
        latitude=D("40"),
        longitude=D(str(LNG0)),
    )
    Place.objects.create(
        state="KS",
        name_key="end town",
        name="End Town",
        latitude=D("40"),
        longitude=D(str(round(lng_at(700), 6))),
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def fake_route(monkeypatch):
    state = {"miles": 700, "calls": 0, "cached": False, "exc": None}

    def fake(start, finish):
        state["calls"] += 1
        if state["exc"]:
            raise state["exc"]
        return straight_route(state["miles"]), state["cached"]

    monkeypatch.setattr(pipeline, "get_route", fake)
    return state


def post(client, start="Start Town, KS", finish="End Town, KS"):
    return client.post(URL, {"start": start, "finish": finish}, format="json")


def test_success_shape_and_values(client, world, fake_route):
    add_station(1, 300, "3.5000")  # only station: needs 200 mi (20 gal) after the free 500
    r = post(client)
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"route", "fuel", "meta"}
    assert d["route"]["start"]["label"] == "Start Town, KS"
    assert d["route"]["distance_miles"] == 700.0
    coords = d["route"]["geometry"]["coordinates"]
    assert d["route"]["geometry"]["type"] == "LineString"
    assert coords[0] == pytest.approx([LNG0, LAT], abs=1e-4)  # GeoJSON = [lng, lat]
    f = d["fuel"]
    assert f["start_gallons"] == "50.000" and f["gallons_consumed"] == "70.000"
    assert f["gallons_purchased"] == "20.000" and f["gallons_remaining_at_destination"] == "0.000"
    assert f["total_fuel_cost"] == "70.00"
    (stop,) = f["stops"]
    assert stop["sequence"] == 1 and stop["station"]["opis_id"] == 1
    assert stop["price_per_gallon"] == "3.5000" and stop["cost"] == "70.00"
    assert stop["gallons_on_arrival"] == "20.000" and stop["gallons_after_purchase"] == "40.000"
    assert stop["mile_marker"] == pytest.approx(300, abs=1)
    assert isinstance(stop["station"]["lat"], float)
    assert d["meta"]["route_cached"] is False and d["meta"]["algorithm"] == "greedy-v1"


def test_short_trip_costs_nothing_but_reports_consumption(client, world, fake_route):
    fake_route["miles"] = 300
    add_station(1, 100, "3.5")
    d = post(client).json()["fuel"]
    assert d["stops"] == [] and d["total_fuel_cost"] == "0.00"
    assert d["gallons_consumed"] == "30.000" and d["gallons_purchased"] == "0.000"
    assert d["gallons_remaining_at_destination"] == "20.000"


def test_750_mile_trip_charges_only_purchased_fuel(client, world, fake_route):
    fake_route["miles"] = 750
    add_station(1, 400, "3.0")
    f = post(client).json()["fuel"]
    assert f["gallons_consumed"] == "75.000" and f["gallons_purchased"] == "25.000"
    assert f["total_fuel_cost"] == "75.00"


def test_receipt_total_is_sum_of_rounded_lines(client, world, fake_route):
    fake_route["miles"] = 1300
    add_station(1, 450, "3.3333")
    add_station(2, 850, "3.3333")
    r = post(client)
    assert r.status_code == 200, r.json()
    f = r.json()["fuel"]
    assert D(f["total_fuel_cost"]) == sum(D(s["cost"]) for s in f["stops"])


def test_station_outside_corridor_excluded_and_inside_included(client, world, fake_route):
    add_station(1, 300, "1.0", off_miles=10.6)  # cheapest but outside the 10 mi corridor
    add_station(2, 300, "3.0", off_miles=9.0)
    d = post(client).json()
    assert [s["station"]["opis_id"] for s in d["fuel"]["stops"]] == [2]
    assert d["fuel"]["stops"][0]["distance_from_route_miles"] == pytest.approx(9.0, abs=0.2)
    assert d["meta"]["candidate_stations"] == 1


def test_station_without_coordinates_never_a_candidate(client, world, fake_route):
    FuelStation.objects.create(
        opis_id=9,
        name="n",
        address="a",
        city="c",
        state="KS",
        rack_id=1,
        price=D("1"),
        updated_at="2026-01-01T00:00:00Z",
    )
    add_station(2, 300, "3.0")
    assert [s["station"]["opis_id"] for s in post(client).json()["fuel"]["stops"]] == [2]


def test_infeasible_plan_is_422_with_details(client, world, fake_route):
    r = post(client)  # 700 mi, no stations at all
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["code"] == "no_feasible_fuel_plan"
    assert e["details"]["max_reachable_mile"] == 500.0 and e["details"]["gap_miles"] == 700.0


def test_lat_lng_inputs_and_query_budget(client, world, fake_route):
    add_station(1, 300, "3.5")
    body = {"start": {"lat": LAT, "lng": LNG0}, "finish": {"lat": LAT, "lng": lng_at(700)}}
    with CaptureQueriesContext(connection) as q:
        r = client.post(URL, body, format="json")
    assert r.status_code == 200
    assert len(q) == 2  # candidates + stop metadata; no Place lookup for coordinates


def test_city_state_path_uses_exactly_three_queries_and_one_provider_call(
    client, world, fake_route
):
    add_station(1, 300, "3.5")
    with CaptureQueriesContext(connection) as q:
        r = post(client)
    assert r.status_code == 200
    assert len(q) == 3  # 1 Place + 1 candidates + 1 selected-stop metadata
    assert fake_route["calls"] == 1


def test_short_trip_skips_metadata_query(client, world, fake_route):
    fake_route["miles"] = 300
    with CaptureQueriesContext(connection) as q:
        post(client)
    assert len(q) == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"start": "Start Town, KS"},
        {"finish": "End Town, KS"},
        {"start": "", "finish": "End Town, KS"},
        {"start": None, "finish": "End Town, KS"},
        {"start": 5, "finish": "End Town, KS"},
        {"start": {"lat": 91, "lng": 0}, "finish": "End Town, KS"},
        {"start": {"lat": "x", "lng": 0}, "finish": "End Town, KS"},
        {"start": {"lat": True, "lng": 0}, "finish": "End Town, KS"},
        {"start": {"lat": 40}, "finish": "End Town, KS"},
        {"start": {"lat": float("nan"), "lng": 0}, "finish": "End Town, KS"},
    ],
)
def test_validation_errors_400(client, world, fake_route, body):
    r = client.post(URL, json.dumps(body, default=str), content_type="application/json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
    assert fake_route["calls"] == 0


def test_malformed_json_400(client, world):
    r = client.post(URL, "{not json", content_type="application/json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_same_start_and_finish_400(client, world, fake_route):
    r = post(client, "Start Town, KS", "start town,  ks")
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
    assert fake_route["calls"] == 0


def test_unknown_city_and_bad_format_422(client, world, fake_route):
    r = post(client, "Nowhereville, KS")
    assert r.status_code == 422 and r.json()["error"]["code"] == "location_not_found"
    r = post(client, "just some words")
    assert r.status_code == 422 and r.json()["error"]["code"] == "location_not_found"
    assert fake_route["calls"] == 0


def test_outside_service_area_422(client, world, fake_route):
    r = post(client, {"lat": 51.5, "lng": -0.12})  # London
    assert r.status_code == 422 and r.json()["error"]["code"] == "location_outside_service_area"
    r = post(client, {"lat": 19.43, "lng": -99.13})  # Mexico City
    assert r.status_code == 422


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (RouteNotFound(), 422, "route_not_found"),
        (RoutingProviderError(), 502, "routing_provider_error"),
        (RoutingTimeout(), 504, "routing_timeout"),
        (RoutingRateLimited(), 503, "routing_rate_limited"),
    ],
)
def test_provider_errors_are_mapped(client, world, fake_route, exc, status, code):
    fake_route["exc"] = exc
    r = post(client)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert "Traceback" not in r.content.decode()
    if status == 503:
        assert r["Retry-After"]


def test_unexpected_error_is_generic_500(client, world, fake_route):
    fake_route["exc"] = RuntimeError("secret internals")
    client.raise_request_exception = False
    r = post(client)
    assert r.status_code == 500
    body = r.content.decode()
    assert r.json()["error"]["code"] == "internal_error" and "secret internals" not in body


def test_db_error_maps_to_503(client, world, fake_route, monkeypatch):
    def boom(*a, **k):
        raise OperationalError("connection refused")

    monkeypatch.setattr(pipeline, "resolve_locations", boom)
    r = post(client)
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"


def test_second_identical_request_is_cached_end_to_end(client, world, settings):
    """Through the real routing module: provider called once, second answer from cache."""
    import responses

    settings.ORS_API_KEY = "k"
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ors_sample.json").read_text())
    pts = decimate(decode_polyline(fixture["routes"][0]["geometry"]), 0.5)
    step = len(pts) // 14
    for n, i in enumerate(range(step, len(pts) - 1, step)):  # a station roughly every 200 miles
        add_station(n + 1, 0, "3.2", lat=pts[i][0])
        FuelStation.objects.filter(opis_id=n + 1).update(
            latitude=D(str(pts[i][0])), longitude=D(str(pts[i][1]))
        )
    body = {"start": {"lat": 40.7128, "lng": -74.006}, "finish": {"lat": 34.0522, "lng": -118.2437}}
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, f"{settings.ORS_BASE_URL}/v2/directions/driving-car", json=fixture)
        first = client.post(URL, body, format="json").json()
        second = client.post(URL, body, format="json").json()
        assert len(rsps.calls) == 1
    assert first["meta"]["route_cached"] is False and second["meta"]["route_cached"] is True
    assert first["fuel"] == second["fuel"]


def test_health(client, db):
    r = client.get("/api/v1/health/")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_method_not_allowed_uses_envelope(client, db):
    r = client.get(URL)
    assert r.status_code == 405 and "error" in r.json()


def test_throttle_returns_429_envelope(client, world, fake_route, settings, monkeypatch):
    from rest_framework.throttling import AnonRateThrottle

    monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"anon": "2/min"})
    add_station(1, 300, "3.5")
    codes = [post(client).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429


# --- performance guards -------------------------------------------------------------------------


def test_real_geometry_with_many_stations_is_fast_and_bounded(client, db, monkeypatch):
    """Real 2,800-mile ORS geometry with 4,000 synthetic stations: no O(n^2), bounded queries."""
    import random
    import time

    data = json.loads((Path(__file__).parent / "fixtures" / "ors_sample.json").read_text())
    pts = decimate(decode_polyline(data["routes"][0]["geometry"]), 0.5)
    rng = random.Random(5)
    stations = []
    for i in range(4000):
        if i % 2:  # half near the route, half scattered across the country
            lat, lng = pts[rng.randrange(len(pts))]
            lat += rng.uniform(-0.05, 0.05)
            lng += rng.uniform(-0.05, 0.05)
        else:
            lat, lng = rng.uniform(26, 48), rng.uniform(-122, -70)
        stations.append(
            FuelStation(
                opis_id=i + 1,
                name="s",
                address="a",
                city="c",
                state="KS",
                rack_id=1,
                price=D(str(round(rng.uniform(2.8, 4.2), 3))),
                latitude=D(str(round(lat, 6))),
                longitude=D(str(round(lng, 6))),
                updated_at="2026-01-01T00:00:00Z",
            )
        )
    FuelStation.objects.bulk_create(stations)
    monkeypatch.setattr(pipeline, "get_route", lambda s, f: (Route(2794.2, 2700, pts), True))
    body = {"start": {"lat": 40.71, "lng": -74.0}, "finish": {"lat": 34.05, "lng": -118.24}}
    with CaptureQueriesContext(connection) as q:
        t = time.perf_counter()
        r = client.post(URL, body, format="json")
        elapsed = time.perf_counter() - t
    assert r.status_code == 200, r.content
    assert len(q) == 2
    assert elapsed < 3.0, f"warm path took {elapsed:.2f}s"
    assert len(r.json()["fuel"]["stops"]) >= 4
