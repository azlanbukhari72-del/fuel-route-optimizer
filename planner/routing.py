"""OpenRouteService client. The only module that knows the provider's JSON.

One directions request per uncached route. Provider payloads are converted to `Route`; the cache
stores our own decimated polyline, never the provider response.

Retry policy (identical in README and tests): at most one retry, only on connection errors,
timeouts and HTTP 502/503/504. Never retried: 400, 401, 403, 404, 429 and anything else.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from .errors import RouteNotFound, RoutingProviderError, RoutingRateLimited, RoutingTimeout
from .geo import decimate, decode_polyline, encode_polyline

logger = logging.getLogger(__name__)

# Bump when provider, profile, request options or the cached payload format change.
ROUTING_CACHE_VERSION = "ors-driving-car-mi-decim0.5-v1"
POLYLINE_PRECISION = 5
DECIMATE_MILES = 0.5
TIMEOUT = (3, 15)  # connect, read seconds
RETRY_STATUSES = {502, 503, 504}
NO_ROUTE_CODES = {2009, 2010}  # ORS: route not found / point not routable


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_minutes: float
    points: list[tuple[float, float]]  # (lat, lng), thinned to ~DECIMATE_MILES spacing


def _cache_key(start, finish) -> str:
    raw = f"{start[0]:.4f},{start[1]:.4f}|{finish[0]:.4f},{finish[1]:.4f}"
    return f"route:{ROUTING_CACHE_VERSION}:{hashlib.sha1(raw.encode()).hexdigest()}"


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> tuple[Route, bool]:
    """Return (route, served_from_cache). start/finish are (lat, lng)."""
    key = _cache_key(start, finish)
    try:
        hit = cache.get(key)
    except Exception:  # cache backend down: degrade to the provider
        logger.warning("route cache read failed", exc_info=True)
        hit = None
    if hit:
        return Route(hit["d"], hit["m"], decode_polyline(hit["p"], POLYLINE_PRECISION)), True
    route = _fetch(start, finish)
    try:
        cache.set(
            key,
            {
                "d": route.distance_miles,
                "m": route.duration_minutes,
                "p": encode_polyline(route.points, POLYLINE_PRECISION),
            },
            settings.ROUTE_CACHE_TTL,
        )
    except Exception:
        logger.warning("route cache write failed", exc_info=True)
    return route, False


def _fetch(start, finish) -> Route:
    if not settings.ORS_API_KEY:
        logger.critical("ORS_API_KEY is not configured")
        raise RoutingProviderError("Routing is not configured.")
    url = f"{settings.ORS_BASE_URL}/v2/directions/driving-car"
    body = {
        "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],  # provider wants [lng, lat]
        "units": "mi",
        "instructions": False,
    }
    headers = {"Authorization": settings.ORS_API_KEY, "Content-Type": "application/json"}

    t0 = time.perf_counter()
    for attempt in (1, 2):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=TIMEOUT)
        except requests.Timeout as e:
            failure: Exception = RoutingTimeout()
            failure.__cause__ = e
            reason = "timeout"
        except requests.ConnectionError as e:
            failure = RoutingProviderError("Could not reach routing provider.")
            failure.__cause__ = e
            reason = "connection_error"
        else:
            if resp.status_code not in RETRY_STATUSES:
                logger.info(
                    "ors status=%s latency_ms=%d",
                    resp.status_code,
                    (time.perf_counter() - t0) * 1000,
                )
                return _parse(resp)
            failure = RoutingProviderError(f"Routing provider unavailable ({resp.status_code}).")
            reason = f"http_{resp.status_code}"
        if attempt == 1:
            logger.warning("ors retry reason=%s", reason)
            time.sleep(0.3 + random.random() * 0.2)
            continue
        logger.error(
            "ors failed reason=%s latency_ms=%d", reason, (time.perf_counter() - t0) * 1000
        )
        raise failure
    raise AssertionError("unreachable")


def _error_code(resp) -> int | None:
    try:
        return int(resp.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


def _parse(resp) -> Route:
    status = resp.status_code
    if status == 429:
        logger.warning("ors rate limited")
        raise RoutingRateLimited()
    if status in (401, 403):
        logger.critical("ors rejected credentials status=%s", status)
        raise RoutingProviderError("Routing provider rejected our credentials.")
    if status == 404 or (status == 400 and _error_code(resp) in NO_ROUTE_CODES):
        raise RouteNotFound()
    if status != 200:
        logger.error("ors unexpected status=%s code=%s", status, _error_code(resp))
        raise RoutingProviderError()
    try:
        route = resp.json()["routes"][0]
        distance = float(route["summary"]["distance"])
        duration = float(route["summary"]["duration"]) / 60  # provider gives seconds
        points = decode_polyline(route["geometry"], POLYLINE_PRECISION)
        if len(points) < 2 or distance <= 0:
            raise ValueError("empty route")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        logger.error("ors malformed response: %s", e)
        raise RoutingProviderError("Routing provider returned an unreadable response.") from e
    return Route(distance, duration, decimate(points, DECIMATE_MILES))
