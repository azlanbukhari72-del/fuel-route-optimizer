import json
import logging
from pathlib import Path

import pytest
import requests
import responses
from django.conf import settings

from planner import routing
from planner.errors import (
    RouteNotFound,
    RoutingProviderError,
    RoutingRateLimited,
    RoutingTimeout,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ors_sample.json").read_text())
URL = f"{settings.ORS_BASE_URL}/v2/directions/driving-car"
NYC, LA = (40.7128, -74.0060), (34.0522, -118.2437)


@pytest.fixture(autouse=True)
def _no_sleep_and_key(monkeypatch, settings):
    monkeypatch.setattr(routing.time, "sleep", lambda s: None)
    settings.ORS_API_KEY = "secret-test-key"


def ok():
    return responses.Response(responses.POST, URL, json=FIXTURE, status=200)


@responses.activate
def test_success_converts_to_internal_route_and_sends_lng_lat():
    responses.add(ok())
    route, cached = routing.get_route(NYC, LA)
    assert cached is False
    assert route.distance_miles == pytest.approx(2794.215)
    assert route.duration_minutes == pytest.approx(161823.4 / 60)  # provider seconds -> minutes
    assert route.points[0] == pytest.approx((40.7, -74.0), abs=0.05)  # internal = (lat, lng)
    assert 1000 < len(route.points) < 8000  # simplified from ~21k provider vertices
    assert len(route.cum_miles) == len(route.points) and route.cum_miles[0] == 0
    assert route.cum_miles == sorted(route.cum_miles)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["coordinates"] == [
        [-74.0060, 40.7128],
        [-118.2437, 34.0522],
    ]  # provider = [lng, lat]
    assert sent["units"] == "mi" and sent["instructions"] is False
    assert responses.calls[0].request.headers["Authorization"] == "secret-test-key"


@responses.activate
def test_second_identical_request_hits_cache_no_provider_call():
    responses.add(ok())
    routing.get_route(NYC, LA)
    route, cached = routing.get_route((40.71281, -74.00601), LA)  # jitter below 4 dp rounding
    assert cached is True and len(responses.calls) == 1
    assert route.distance_miles == pytest.approx(2794.215)


@responses.activate
def test_cache_key_includes_routing_version(monkeypatch):
    responses.add(ok())
    routing.get_route(NYC, LA)
    monkeypatch.setattr(routing, "ROUTING_CACHE_VERSION", "bumped")
    _, cached = routing.get_route(NYC, LA)
    assert cached is False and len(responses.calls) == 2


@responses.activate
def test_cache_failure_degrades_to_provider(monkeypatch):
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("redis down")

        set = get

    monkeypatch.setattr(routing, "cache", Boom())
    responses.add(ok())
    route, cached = routing.get_route(NYC, LA)
    assert cached is False and route.distance_miles > 0


# --- retry policy: one retry on conn error/timeout/502/503/504; none on 400/401/403/404/429/500 ---


@pytest.mark.parametrize("status", [502, 503, 504])
@responses.activate
def test_retried_once_on_transient_status_then_provider_error(status):
    responses.add(responses.POST, URL, status=status, json={})
    with pytest.raises(RoutingProviderError):
        routing.get_route(NYC, LA)
    assert len(responses.calls) == 2


@pytest.mark.parametrize("status", [502, 503, 504])
@responses.activate
def test_retry_can_succeed(status):
    responses.add(responses.POST, URL, status=status, json={})
    responses.add(ok())
    route, _ = routing.get_route(NYC, LA)
    assert route.distance_miles > 0 and len(responses.calls) == 2


@responses.activate
def test_timeout_retried_once_then_timeout_error():
    responses.add(responses.POST, URL, body=requests.Timeout("slow"))
    with pytest.raises(RoutingTimeout):
        routing.get_route(NYC, LA)
    assert len(responses.calls) == 2


@responses.activate
def test_connection_error_retried_once_then_provider_error():
    responses.add(responses.POST, URL, body=requests.ConnectionError("down"))
    with pytest.raises(RoutingProviderError):
        routing.get_route(NYC, LA)
    assert len(responses.calls) == 2


@pytest.mark.parametrize(
    "status,body,exc",
    [
        (400, {"error": {"code": 2010}}, RouteNotFound),
        (400, {"error": {"code": 2009}}, RouteNotFound),
        (404, {"error": {"code": 2010}}, RouteNotFound),
        (400, {"error": {"code": 2003}}, RoutingProviderError),  # our request was invalid: a bug
        (401, {}, RoutingProviderError),
        (403, {}, RoutingProviderError),
        (429, {}, RoutingRateLimited),
        (500, {}, RoutingProviderError),
    ],
)
@responses.activate
def test_never_retried(status, body, exc):
    responses.add(responses.POST, URL, status=status, json=body)
    with pytest.raises(exc):
        routing.get_route(NYC, LA)
    assert len(responses.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"routes": []},
        {"routes": [{"summary": {"distance": 5}}]},
        {"routes": [{"summary": {"distance": "x", "duration": 1}, "geometry": "abc"}]},
        {"routes": [{"summary": {"distance": 5, "duration": 1}, "geometry": ""}]},
    ],
)
@responses.activate
def test_malformed_provider_body(payload):
    responses.add(responses.POST, URL, json=payload)
    with pytest.raises(RoutingProviderError):
        routing.get_route(NYC, LA)


@responses.activate
def test_non_json_200_body():
    responses.add(responses.POST, URL, body="<html>oops</html>", status=200)
    with pytest.raises(RoutingProviderError):
        routing.get_route(NYC, LA)


def test_missing_api_key(settings):
    settings.ORS_API_KEY = ""
    with pytest.raises(RoutingProviderError):
        routing.get_route(NYC, LA)


@responses.activate
def test_api_key_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    responses.add(responses.POST, URL, status=503, json={})
    with pytest.raises(RoutingProviderError):
        routing.get_route(NYC, LA)
    assert "secret-test-key" not in caplog.text


# --- cached payload validation ---------------------------------------------------------------


def _good_payload():
    from django.core.cache import cache

    with responses.RequestsMock() as rsps:
        rsps.add(ok())
        routing.get_route(NYC, LA)
    return cache.get(routing._cache_key(NYC, LA))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: "not a dict",
        lambda p: 12345,
        lambda p: {},
        lambda p: {k: v for k, v in p.items() if k != "p"},
        lambda p: {**p, "p": "\x00\x01garbage-not-a-polyline"},
        lambda p: {**p, "p": ""},
        lambda p: {**p, "d": "nan"},
        lambda p: {**p, "d": -5},
        lambda p: {**p, "c": p["c"][:-1]},  # length mismatch
        lambda p: {**p, "c": list(reversed(p["c"]))},  # not monotonic
        lambda p: {**p, "c": None},
        lambda p: {**p, "m": None},
    ],
)
@responses.activate
def test_corrupt_cache_entry_is_a_miss_and_is_replaced(mutate, caplog):
    from django.core.cache import cache

    good = _good_payload()
    key = routing._cache_key(NYC, LA)
    cache.set(key, mutate(good), 60)
    responses.add(ok())
    caplog.set_level(logging.WARNING)
    route, cached = routing.get_route(NYC, LA)
    assert cached is False and route.distance_miles == pytest.approx(2794.215)
    assert len(responses.calls) == 1  # recovered by asking the provider
    assert "route cache entry invalid" in caplog.text
    assert "secret-test-key" not in caplog.text
    _, cached_again = routing.get_route(NYC, LA)  # the bad entry was overwritten
    assert cached_again is True and len(responses.calls) == 1


@responses.activate
def test_valid_cached_entry_round_trips_cum_miles():
    responses.add(ok())
    first, _ = routing.get_route(NYC, LA)
    second, cached = routing.get_route(NYC, LA)
    assert cached and len(second.points) == len(first.points)
    assert second.cum_miles == pytest.approx(first.cum_miles, abs=1e-3)


@responses.activate
def test_fresh_route_is_identical_to_cached_route():
    responses.add(ok())
    fresh, cached1 = routing.get_route(NYC, LA)
    hit, cached2 = routing.get_route(NYC, LA)
    assert (cached1, cached2) == (False, True)
    assert fresh == hit  # same points, same mileage, same distance: plans cannot differ
