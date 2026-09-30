"""Application errors and the single JSON error envelope: {"error": {code, message, details}}."""

import logging

from django.db import OperationalError
from rest_framework import exceptions
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class AppError(Exception):
    status = 500
    code = "internal_error"
    message = "Unexpected error."
    headers: dict[str, str] | None = None

    def __init__(self, message: str | None = None, details: dict | None = None):
        self.message = message or self.message
        self.details = details or {}
        super().__init__(self.message)


class SameLocation(AppError):
    status, code, message = 400, "validation_error", "start and finish must be different places."


class LocationNotFound(AppError):
    status, code, message = 422, "location_not_found", "Location could not be resolved."


class LocationOutsideServiceArea(AppError):
    status, code = 422, "location_outside_service_area"
    message = "Location is outside the supported contiguous-US area."


class RouteNotFound(AppError):
    status, code, message = 422, "route_not_found", "No driving route found between the locations."


class NoFeasibleFuelPlan(AppError):
    status, code = 422, "no_feasible_fuel_plan"
    message = "No sequence of fuel stops can complete this route within the vehicle range."


class RoutingRateLimited(AppError):
    status, code = 503, "routing_rate_limited"
    message = "Routing provider is rate limiting requests; retry shortly."

    def __init__(self, message=None, details=None, retry_after: int = 30):
        super().__init__(message, details)
        self.headers = {"Retry-After": str(retry_after)}


class RoutingProviderError(AppError):
    status, code, message = 502, "routing_provider_error", "Routing provider returned an error."


class RoutingTimeout(AppError):
    status, code, message = 504, "routing_timeout", "Routing provider timed out."


def _envelope(status: int, code: str, message: str, details=None, headers=None) -> Response:
    return Response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=status,
        headers=headers,
    )


def exception_handler(exc, context):
    if isinstance(exc, AppError):
        return _envelope(exc.status, exc.code, exc.message, exc.details, exc.headers)
    if isinstance(exc, exceptions.ValidationError):
        return _envelope(400, "validation_error", "Invalid request.", exc.detail)
    if isinstance(exc, exceptions.Throttled):
        wait = int(exc.wait or 1)
        return _envelope(
            429,
            "throttled",
            "Too many requests.",
            {"retry_after": wait},
            {"Retry-After": str(wait)},
        )
    if isinstance(exc, exceptions.ParseError):
        return _envelope(400, "validation_error", "Malformed JSON body.")
    if isinstance(exc, exceptions.APIException):
        return _envelope(exc.status_code, exc.default_code, str(exc.detail))
    if isinstance(exc, OperationalError):
        logger.error("database unavailable: %s", exc)
        return _envelope(503, "database_unavailable", "Database is temporarily unavailable.")
    logger.exception("unhandled error")
    return _envelope(500, "internal_error", "Internal server error.")
