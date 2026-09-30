"""Startup configuration validation: bad values must fail fast, never reach a request."""

import os
import runpy
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

SETTINGS = Path(__file__).resolve().parent.parent / "config" / "settings.py"


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch):
    # settings.py loads .env with setdefault(); work on a copy so nothing leaks into other tests
    monkeypatch.setattr(os, "environ", dict(os.environ))


def load(monkeypatch, **env):
    """Execute a fresh copy of settings.py with the given environment."""
    for key in [
        "VEHICLE_RANGE_MILES", "VEHICLE_MPG", "STATION_CORRIDOR_MILES", "ROUTE_CACHE_TTL",
        "ORS_CONNECT_TIMEOUT", "ORS_READ_TIMEOUT", "ORS_MAX_RETRIES", "PLAN_THROTTLE_RATE",
        "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "SECRET_KEY",
    ]:  # fmt: skip
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEBUG", "True")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return runpy.run_path(str(SETTINGS))


def test_defaults_are_valid(monkeypatch):
    s = load(monkeypatch)
    assert s["VEHICLE_RANGE_MILES"] == 500 and s["VEHICLE_MPG"] == 10
    assert s["STATION_CORRIDOR_MILES"] == 10 and s["ROUTE_CACHE_TTL"] == 86400
    assert (s["ORS_CONNECT_TIMEOUT"], s["ORS_READ_TIMEOUT"], s["ORS_MAX_RETRIES"]) == (3, 15, 1)
    assert s["REST_FRAMEWORK"]["DEFAULT_THROTTLE_RATES"] == {"anon": "30/min"}
    assert s["ALLOWED_HOSTS"] == ["localhost", "127.0.0.1"] and s["CSRF_TRUSTED_ORIGINS"] == []


@pytest.mark.parametrize(
    "name,value",
    [
        ("VEHICLE_RANGE_MILES", "0"),
        ("VEHICLE_RANGE_MILES", "-5"),
        ("VEHICLE_RANGE_MILES", "nan"),
        ("VEHICLE_RANGE_MILES", "inf"),
        ("VEHICLE_RANGE_MILES", "five hundred"),
        ("VEHICLE_MPG", "0"),
        ("VEHICLE_MPG", "-1"),
        ("STATION_CORRIDOR_MILES", "0"),
        ("STATION_CORRIDOR_MILES", "500"),  # absurdly wide corridor
        ("ROUTE_CACHE_TTL", "0"),
        ("ROUTE_CACHE_TTL", "-60"),
        ("ROUTE_CACHE_TTL", "1.5"),  # must be an integer number of seconds
        ("ORS_CONNECT_TIMEOUT", "0"),
        ("ORS_READ_TIMEOUT", "-1"),
        ("ORS_READ_TIMEOUT", "9999"),
        ("ORS_MAX_RETRIES", "-1"),
        ("ORS_MAX_RETRIES", "50"),
        ("PLAN_THROTTLE_RATE", "fast"),
        ("PLAN_THROTTLE_RATE", "0/min"),
        ("PLAN_THROTTLE_RATE", "30/fortnight"),
        ("PLAN_THROTTLE_RATE", ""),
    ],
)
def test_invalid_values_fail_at_startup_naming_the_variable(monkeypatch, name, value):
    with pytest.raises(ImproperlyConfigured, match=name):
        load(monkeypatch, **{name: value})


@pytest.mark.parametrize("rate", ["30/min", "5/second", "1000/hour", "10/d", "60/minute"])
def test_valid_throttle_rates(monkeypatch, rate):
    assert load(monkeypatch, PLAN_THROTTLE_RATE=rate)["REST_FRAMEWORK"][
        "DEFAULT_THROTTLE_RATES"
    ] == {"anon": rate}


def test_custom_valid_values_are_read(monkeypatch):
    s = load(
        monkeypatch,
        VEHICLE_RANGE_MILES="450.5",
        VEHICLE_MPG="8",
        ROUTE_CACHE_TTL="60",
        ORS_MAX_RETRIES="0",
        CSRF_TRUSTED_ORIGINS="https://a.example.com, https://b.example.com",
        ALLOWED_HOSTS="a.example.com,b.example.com",
    )
    assert s["VEHICLE_RANGE_MILES"] == 450.5 and s["VEHICLE_MPG"] == 8
    assert s["ROUTE_CACHE_TTL"] == 60 and s["ORS_MAX_RETRIES"] == 0
    assert s["CSRF_TRUSTED_ORIGINS"] == ["https://a.example.com", "https://b.example.com"]
    assert s["ALLOWED_HOSTS"] == ["a.example.com", "b.example.com"]


def test_production_requires_secret_key_and_allowed_hosts(monkeypatch):
    load(monkeypatch)  # clears env first
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        runpy.run_path(str(SETTINGS))
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("ALLOWED_HOSTS", "")
    with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
        runpy.run_path(str(SETTINGS))
    monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com")
    monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://api.example.com")
    s = runpy.run_path(str(SETTINGS))
    assert s["DEBUG"] is False and s["SECURE_CONTENT_TYPE_NOSNIFF"] is True
    assert s["CSRF_TRUSTED_ORIGINS"] == ["https://api.example.com"]
