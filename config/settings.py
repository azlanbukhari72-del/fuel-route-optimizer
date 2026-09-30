"""Single-file, environment-driven settings."""

import math
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    # Tiny .env reader so local dev needs no extra dependency; real env vars win.
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes"}


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    return default if raw is None else [x.strip() for x in raw.split(",") if x.strip()]


def env_number(name: str, default, cast=float, *, gt=None, ge=None, le=None):
    """Read a numeric env var and fail fast with a clear message if it is invalid."""
    raw = os.environ.get(name, str(default))
    try:
        value = cast(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ImproperlyConfigured(f"{name} must be finite, got {raw!r}")
    if gt is not None and not value > gt:
        raise ImproperlyConfigured(f"{name} must be greater than {gt}, got {value}")
    if ge is not None and not value >= ge:
        raise ImproperlyConfigured(f"{name} must be at least {ge}, got {value}")
    if le is not None and not value <= le:
        raise ImproperlyConfigured(f"{name} must be at most {le}, got {value}")
    return value


def env_rate(name: str, default: str) -> str:
    """DRF throttle rate like "30/min"; validated so a typo can't surface at request time."""
    raw = os.environ.get(name, default).strip()
    m = re.fullmatch(r"(\d+)/(s|sec|second|m|min|minute|h|hour|d|day)", raw)
    if not m or int(m[1]) < 1:
        raise ImproperlyConfigured(
            f'{name} must look like "30/min" (N/second|minute|hour|day), got {raw!r}'
        )
    return raw


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("SECRET_KEY must be set when DEBUG is off")
    SECRET_KEY = "dev-only-insecure-key"
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
# Only matters if a browser/session feature is added; this JSON API has no CSRF-protected views.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", [])
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not be empty when DEBUG is off")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "planner",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES: list = []


def _database() -> dict:
    url = urlparse(
        os.environ.get("DATABASE_URL", "postgres://postgres@localhost:5432/fuel_station")
    )
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(url.path.lstrip("/")),
        "USER": unquote(url.username or ""),
        "PASSWORD": unquote(url.password or ""),
        "HOST": url.hostname or "localhost",
        "PORT": url.port or 5432,
        "CONN_MAX_AGE": 60,
    }


DATABASES = {"default": _database()}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

USE_TZ = True
TIME_ZONE = "UTC"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": env_rate("PLAN_THROTTLE_RATE", "30/min")},
    "EXCEPTION_HANDLER": "planner.errors.exception_handler",
    "UNAUTHENTICATED_USER": None,
}

# --- Domain configuration (validated at startup: invalid values never reach a request) -----
VEHICLE_RANGE_MILES = env_number("VEHICLE_RANGE_MILES", 500, gt=0)
VEHICLE_MPG = env_number("VEHICLE_MPG", 10, gt=0)
STATION_CORRIDOR_MILES = env_number("STATION_CORRIDOR_MILES", 10, gt=0, le=100)
ROUTE_CACHE_TTL = env_number("ROUTE_CACHE_TTL", 86400, int, gt=0)
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
ORS_BASE_URL = os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org")
ORS_CONNECT_TIMEOUT = env_number("ORS_CONNECT_TIMEOUT", 3, gt=0, le=60)  # seconds
ORS_READ_TIMEOUT = env_number("ORS_READ_TIMEOUT", 15, gt=0, le=120)  # seconds
ORS_MAX_RETRIES = env_number("ORS_MAX_RETRIES", 1, int, ge=0, le=3)  # transient failures only

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"planner": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
