"""The request pipeline: resolve -> route (cached) -> candidates -> optimize -> response data."""

import logging
import time
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from .errors import NoFeasibleFuelPlan, SameLocation
from .geo import haversine_miles, simplify
from .locations import resolve_locations
from .models import FuelStation
from .optimizer import NoFeasiblePlan, plan_fuel_stops
from .routing import get_route
from .stations import select_candidates

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
GEOMETRY_TOLERANCE_MILES = 0.25
ASSUMPTIONS = [
    "vehicle starts with a full tank whose fuel is already paid for (not in total_fuel_cost)",
    "only fuel bought at stations is charged; gallons_consumed is reported separately",
    "station locations are city centroids; corridor tolerance applies",
    "duplicate price rows for one station are reduced to the lowest price",
]


def plan_trip(raw_start, raw_finish) -> dict:
    t0 = time.perf_counter()
    start, finish = resolve_locations(raw_start, raw_finish)
    if haversine_miles(start.point, finish.point) < 0.1:
        raise SameLocation()

    t1 = time.perf_counter()
    route, cached = get_route(start.point, finish.point)
    t2 = time.perf_counter()
    corridor = settings.STATION_CORRIDOR_MILES
    selection = select_candidates(route, corridor)
    t3 = time.perf_counter()
    try:
        plan = plan_fuel_stops(
            route.distance_miles,
            selection.candidates,
            range_miles=settings.VEHICLE_RANGE_MILES,
            mpg=settings.VEHICLE_MPG,
        )
    except NoFeasiblePlan as e:
        raise NoFeasibleFuelPlan(
            details={
                "stuck_at_mile": round(e.stuck_at_mile, 1),
                "max_reachable_mile": round(e.max_reachable_mile, 1),
                "next_stop_mile": round(e.next_mile, 1),
                "gap_miles": round(e.gap_miles, 1),
            }
        ) from e
    t4 = time.perf_counter()

    stations = FuelStation.objects.in_bulk([s.candidate.id for s in plan.stops])
    stops = []
    total = Decimal(0)
    for n, s in enumerate(plan.stops, start=1):
        st = stations[s.candidate.id]
        cost = s.cost.quantize(CENT, rounding=ROUND_HALF_UP)  # receipt line
        total += cost
        stops.append(
            {
                "sequence": n,
                "station": {
                    "opis_id": st.opis_id,
                    "name": st.name,
                    "address": st.address,
                    "city": st.city,
                    "state": st.state,
                    "lat": float(st.latitude),
                    "lng": float(st.longitude),
                },
                "mile_marker": round(s.candidate.pos, 1),
                "distance_from_route_miles": round(selection.off_route_miles[st.id], 1),
                "price_per_gallon": st.price,
                "gallons_on_arrival": s.gallons_on_arrival,
                "gallons_purchased": s.gallons_purchased,
                "gallons_after_purchase": s.gallons_on_arrival + s.gallons_purchased,
                "cost": cost,
            }
        )

    shape = simplify(route.points, GEOMETRY_TOLERANCE_MILES)
    data = {
        "route": {
            "start": {"label": start.label, "lat": start.lat, "lng": start.lng},
            "finish": {"label": finish.label, "lat": finish.lat, "lng": finish.lng},
            "distance_miles": round(route.distance_miles, 1),
            "duration_minutes": round(route.duration_minutes),
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(lng, 5), round(lat, 5)] for lat, lng in shape],
            },
        },
        "fuel": {
            "vehicle": {
                "range_miles": settings.VEHICLE_RANGE_MILES,
                "mpg": settings.VEHICLE_MPG,
                "tank_gallons": plan.start_gallons,
            },
            "start_gallons": plan.start_gallons,
            "gallons_consumed": plan.gallons_consumed,
            "gallons_purchased": plan.gallons_purchased,
            "gallons_remaining_at_destination": plan.gallons_remaining,
            "total_fuel_cost": total,
            "stops": stops,
        },
        "meta": {
            "algorithm": "greedy-v1",
            "candidate_stations": len(selection.candidates),
            "route_cached": cached,
            "corridor_miles": corridor,
            "assumptions": ASSUMPTIONS,
        },
    }
    t5 = time.perf_counter()
    logger.info(
        "route_plan ok miles=%.1f considered=%d candidates=%d stops=%d total_cost=%s "
        "route_cached=%s "
        "t_resolve_ms=%d t_route_ms=%d t_select_ms=%d t_optimize_ms=%d t_total_ms=%d",
        route.distance_miles,
        selection.considered,
        len(selection.candidates),
        len(stops),
        total,
        cached,
        (t1 - t0) * 1000,
        (t2 - t1) * 1000,
        (t3 - t2) * 1000,
        (t4 - t3) * 1000,
        (t5 - t0) * 1000,
    )
    return data
