"""Fuel-stop optimizer. Pure Python: no Django, no I/O.

Model (see README "Algorithm"): a vehicle drives a line of length ``route_miles``.
It starts with a FULL tank that is already paid for. The start is NOT a station: nothing is
ever bought there. Real stations sit at ``pos`` miles along the route with a price per gallon.
Goal: minimise the money spent on fuel bought at stations (only objective; ties are broken by
fixed rules for determinism, not by a secondary goal such as fewest stops).

Everything internal is unrounded. Distances/fuel are float miles(-of-range); money is Decimal.
Rounding is the serializer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

EPS = 1e-6  # float tolerance in miles for range comparisons


@dataclass(frozen=True)
class Candidate:
    id: int
    pos: float  # miles from route start
    price: Decimal  # dollars per gallon


@dataclass(frozen=True)
class Stop:
    candidate: Candidate
    gallons_on_arrival: Decimal
    gallons_purchased: Decimal
    cost: Decimal  # unrounded


@dataclass(frozen=True)
class FuelPlan:
    stops: list[Stop]
    start_gallons: Decimal
    gallons_consumed: Decimal
    gallons_purchased: Decimal
    gallons_remaining: Decimal
    total_cost: Decimal  # unrounded sum of purchase costs


class NoFeasiblePlan(Exception):
    def __init__(self, stuck_at_mile: float, max_reachable_mile: float, next_mile: float):
        self.stuck_at_mile = stuck_at_mile
        self.max_reachable_mile = max_reachable_mile
        self.next_mile = next_mile  # next station (or the destination) that cannot be reached
        super().__init__(
            f"no station within range after mile {stuck_at_mile:.1f}; "
            f"next stop at mile {next_mile:.1f}, reach ends at {max_reachable_mile:.1f}"
        )

    @property
    def gap_miles(self) -> float:
        return self.next_mile - self.stuck_at_mile


def _gal(miles: float, mpg: float) -> Decimal:
    return Decimal(str(miles)) / Decimal(str(mpg))


def plan_fuel_stops(
    route_miles: float,
    stations: list[Candidate],
    range_miles: float = 500.0,
    mpg: float = 10.0,
) -> FuelPlan:
    """Greedy optimal refuelling plan (exchange argument in README).

    Terms: ``cur`` = current station (None only at the start); ``fuel`` = miles of range in the
    tank now; reachable stations = those after ``cur`` within one tank (or within ``fuel`` at the
    start, where buying is impossible); first cheaper station = nearest reachable station (by
    route position) priced strictly below ``cur``; cheapest reachable = min (price, -pos, id).
    """
    R = range_miles
    cands = sorted(
        (c for c in stations if -EPS <= c.pos < route_miles - EPS),
        key=lambda c: (c.pos, c.price, c.id),
    )
    idx = -1  # index of cur in cands; -1 = start
    pos = 0.0
    fuel = R
    stops: list[Stop] = []

    while True:
        cur = cands[idx] if idx >= 0 else None
        if route_miles - pos <= fuel + EPS:  # destination reachable on what we have
            break

        limit = pos + (R if cur else fuel)
        window = []
        for j in range(idx + 1, len(cands)):
            if cands[j].pos > limit + EPS:
                break
            window.append(j)

        arrive_gap_ok = route_miles - pos <= R + EPS
        if cur is not None:
            first_cheaper = next((j for j in window if cands[j].price < cur.price), None)
        else:
            first_cheaper = None

        buy = 0.0
        if first_cheaper is not None:
            target = first_cheaper
            buy = max(0.0, cands[target].pos - pos - fuel)
        elif cur is not None and arrive_gap_ok:
            # nothing cheaper ahead and the finish is within a tank: buy just enough and finish
            buy = route_miles - pos - fuel
            target = None
        elif not window:
            nxt = cands[idx + 1].pos if idx + 1 < len(cands) else route_miles
            raise NoFeasiblePlan(pos, limit, min(nxt, route_miles))
        else:
            target = min(window, key=lambda j: (cands[j].price, -cands[j].pos, cands[j].id))
            if cur is not None:
                buy = R - fuel  # nothing cheaper within a tank: fill up

        if cur is not None and buy > EPS:
            # the purchase happens at cur; record it on cur's stop (created on arrival below)
            stops[-1] = _with_purchase(stops[-1], buy, mpg)
            fuel += buy
        if target is None:
            break
        fuel -= cands[target].pos - pos
        pos = cands[target].pos
        idx = target
        stops.append(
            Stop(cands[target], _gal(max(fuel, 0.0), mpg), Decimal(0), Decimal(0))
        )

    remaining = max(0.0, fuel - (route_miles - pos))
    purchased = sum((s.gallons_purchased for s in stops), Decimal(0))
    return FuelPlan(
        stops=[s for s in stops if s.gallons_purchased > 0],
        start_gallons=_gal(R, mpg),
        gallons_consumed=_gal(route_miles, mpg),
        gallons_purchased=purchased,
        gallons_remaining=_gal(remaining, mpg),
        total_cost=sum((s.cost for s in stops), Decimal(0)),
    )


def _with_purchase(stop: Stop, miles: float, mpg: float) -> Stop:
    gallons = _gal(miles, mpg)
    return Stop(
        stop.candidate,
        stop.gallons_on_arrival,
        stop.gallons_purchased + gallons,
        stop.cost + gallons * stop.candidate.price,
    )
