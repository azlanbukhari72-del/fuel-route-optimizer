import random
from decimal import Decimal as D

import pytest

from planner.optimizer import Candidate, NoFeasiblePlan, plan_fuel_stops


def C(id, pos, price):
    return Candidate(id, float(pos), D(str(price)))


def approx(x):
    return pytest.approx(float(x), abs=1e-9)


def assert_invariants(plan, route_miles, mpg=10.0):
    start, cons, buy, rem = (
        plan.start_gallons,
        plan.gallons_consumed,
        plan.gallons_purchased,
        plan.gallons_remaining,
    )
    assert abs(float(start + buy - cons - rem)) < 1e-9
    assert rem >= 0
    assert abs(float(cons) - route_miles / mpg) < 1e-9
    for s in plan.stops:
        assert s.gallons_on_arrival >= 0
        assert s.gallons_on_arrival + s.gallons_purchased <= plan.start_gallons + D("1e-9")
        assert s.cost == s.gallons_purchased * s.candidate.price


# --- required business rules -------------------------------------------------------------


def test_zero_distance():
    plan = plan_fuel_stops(0, [C(1, 0, 3)])
    assert plan.stops == [] and plan.total_cost == 0 and plan.gallons_consumed == 0


def test_short_trip_free_and_consumption_reported():
    plan = plan_fuel_stops(300, [C(1, 100, 3.0)])
    assert plan.stops == []
    assert plan.total_cost == 0
    assert plan.gallons_purchased == 0
    assert plan.gallons_consumed == 30
    assert plan.start_gallons == 50
    assert float(plan.gallons_remaining) == approx(20)
    assert_invariants(plan, 300)


def test_exact_range_needs_no_stop():
    plan = plan_fuel_stops(500, [C(1, 250, 3.0)])
    assert plan.stops == [] and float(plan.gallons_remaining) == approx(0)


def test_just_over_range_needs_stop():
    plan = plan_fuel_stops(500.1, [C(1, 250, 3.0)])
    assert len(plan.stops) == 1
    assert float(plan.stops[0].gallons_purchased) == approx("0.01")
    assert_invariants(plan, 500.1)


def test_750_miles_only_purchased_fuel_charged():
    plan = plan_fuel_stops(750, [C(1, 400, 3.0)])
    assert plan.gallons_consumed == 75
    assert float(plan.gallons_purchased) == approx(25)
    assert float(plan.total_cost) == approx(75)
    assert float(plan.stops[0].gallons_on_arrival) == approx(10)
    assert_invariants(plan, 750)


def test_start_is_never_a_purchase_point():
    # a station at mile 0 is a real station; without it nothing is bought at the start
    plan = plan_fuel_stops(700, [C(1, 300, 4.0)])
    assert [s.candidate.id for s in plan.stops] == [1]
    assert float(plan.stops[0].gallons_on_arrival) == approx(20)  # 200 of the free 500 left
    assert float(plan.gallons_purchased) == approx(20)  # 700 - 500 = 200 miles = 20 gal, no more


def test_buy_just_enough_to_reach_cheaper_station():
    # at station 1 (mile 300) fuel is 200; reaching mile 600 needs 300 -> buy 100 miles = 10 gal
    stations = [C(1, 300, 4.0), C(2, 600, 3.0)]
    plan = plan_fuel_stops(900, stations)
    p1, p2 = plan.stops
    assert p1.candidate.id == 1 and float(p1.gallons_purchased) == approx(10)
    assert p2.candidate.id == 2 and float(p2.gallons_purchased) == approx(30)  # 900-600 = 300 miles
    assert_invariants(plan, 900)


def test_cheaper_station_beyond_one_tank_is_not_waited_for():
    # station 3 (cheap) is > 500 mi from station 1, so station 1 must fill up rather than wait
    stations = [C(1, 100, 4.0), C(2, 300, 4.5), C(3, 750, 1.0)]
    plan = plan_fuel_stops(1200, stations)
    assert_invariants(plan, 1200)
    first = plan.stops[0]
    assert first.candidate.id == 1
    assert float(first.gallons_on_arrival + first.gallons_purchased) == approx(50)  # filled up
    assert plan.stops[-1].candidate.id == 3  # the cheap station supplies the last leg


def test_expensive_station_used_when_needed_for_reachability():
    plan = plan_fuel_stops(900, [C(1, 400, 9.99)])
    assert [s.candidate.id for s in plan.stops] == [1]
    assert_invariants(plan, 900)


def test_expensive_then_cheap():
    stations = [C(1, 200, 5.0), C(2, 400, 3.0)]
    plan = plan_fuel_stops(800, stations)
    # station 1 is passed on the free tank; all fuel needed comes from the cheap station 2
    assert [s.candidate.id for s in plan.stops] == [2]
    assert float(plan.stops[0].gallons_purchased) == approx(30)


def test_multiple_refuels_long_trip():
    stations = [C(i, i * 300, 3 + (i % 3) * 0.1) for i in range(1, 10)]
    plan = plan_fuel_stops(2800, stations)
    assert len(plan.stops) >= 4
    assert_invariants(plan, 2800)


def test_no_stations_long_trip_is_infeasible():
    with pytest.raises(NoFeasiblePlan) as e:
        plan_fuel_stops(800, [])
    assert e.value.max_reachable_mile == 500 and e.value.next_mile == 800


def test_gap_exactly_range_is_feasible_but_slightly_more_is_not():
    assert float(plan_fuel_stops(1000, [C(1, 500, 3)]).gallons_purchased) == approx(50)
    with pytest.raises(NoFeasiblePlan):
        plan_fuel_stops(1000.01, [C(1, 500.01, 3)])  # first station beyond the free tank


def test_station_at_start_and_at_destination():
    plan = plan_fuel_stops(900, [C(1, 0, 3.0), C(2, 450, 3.5), C(3, 900, 1.0)])
    assert_invariants(plan, 900)
    assert [s.candidate.id for s in plan.stops] == [2]  # mile-0 tank is full; station at D useless


def test_only_station_at_mile_zero_cannot_extend_range():
    with pytest.raises(NoFeasiblePlan):
        plan_fuel_stops(900, [C(1, 0, 3.0)])


def test_equal_prices_deterministic_and_order_independent():
    stations = [C(1, 200, 3.0), C(2, 450, 3.0), C(3, 480, 3.0), C(4, 900, 3.0)]
    a = plan_fuel_stops(1300, stations)
    shuffled = stations[:]
    random.Random(1).shuffle(shuffled)
    b = plan_fuel_stops(1300, shuffled)
    assert [s.candidate.id for s in a.stops] == [s.candidate.id for s in b.stops]
    assert float(a.total_cost) == approx(b.total_cost)
    assert_invariants(a, 1300)


def test_clustered_stations_cheapest_wins():
    stations = [C(1, 400, 3.5), C(2, 400, 3.1), C(3, 400, 3.9)]
    plan = plan_fuel_stops(700, stations)
    assert [s.candidate.id for s in plan.stops] == [2]


def test_duplicate_identical_stations():
    plan = plan_fuel_stops(700, [C(1, 400, 3.0), C(1, 400, 3.0)])
    assert len(plan.stops) == 1


def test_price_precision_is_not_rounded_in_optimizer():
    plan = plan_fuel_stops(700, [C(1, 400, "3.2990")])
    # 200 miles = 20 gal; unrounded cost 20 * 3.2990
    assert float(plan.total_cost) == approx("65.98")
    plan2 = plan_fuel_stops(500.07, [C(1, 100, "3.3333")])
    # 0.07 mi / 10 = 0.007 gal -> cost not quantized
    assert float(plan2.total_cost) == approx(D("0.007") * D("3.3333"))


def test_rounding_does_not_change_decisions():
    # prices differ by less than a cent per gallon; the cheaper one must still win
    plan = plan_fuel_stops(700, [C(1, 400, "3.29901"), C(2, 400, "3.29899")])
    assert plan.stops[0].candidate.id == 2


# --- cross-check against an independent DP on discretised instances -------------------------


def dp_min_cost(route, stations, R):
    """Integer miles/cents. state (station i, fuel f). Returns min cost in cents*miles/mpg units."""
    st = sorted((c for c in stations if 0 <= c[0] < route), key=lambda t: (t[0], t[1]))
    INF = float("inf")
    n = len(st)
    best = [[INF] * (R + 1) for _ in range(n)]
    ans = INF
    for j, (p, _) in enumerate(st):  # leave start with the free full tank
        if p <= R:
            best[j][R - p] = 0
    if route <= R:
        return 0
    for i, (p, price) in enumerate(st):
        for f in range(R):  # buy one mile-unit at a time (cost price per mile)
            if best[i][f] < INF:
                best[i][f + 1] = min(best[i][f + 1], best[i][f] + price)
        for f in range(R + 1):
            if best[i][f] == INF:
                continue
            if route - p <= f:
                ans = min(ans, best[i][f])
            for j in range(i + 1, n):
                d = st[j][0] - p
                if d > f:
                    break
                best[j][f - d] = min(best[j][f - d], best[i][f])
    return ans


def random_instance(rng):
    R = 50  # small integer range keeps the DP tiny; mpg=1 so gallons == miles
    route = rng.randint(1, 260)
    stations = [(rng.randint(0, route), rng.randint(100, 400)) for _ in range(rng.randint(0, 12))]
    return R, route, stations


def greedy_cost_cents(R, route, stations):
    cands = [Candidate(i, float(p), D(price) / 100) for i, (p, price) in enumerate(stations)]
    plan = plan_fuel_stops(route, cands, range_miles=R, mpg=1.0)
    return float(plan.total_cost) * 100, plan


def test_greedy_matches_dp_on_random_discretised_instances():
    rng = random.Random(20260930)
    feasible = 0
    for _ in range(400):
        R, route, stations = random_instance(rng)
        expected = dp_min_cost(route, stations, R)
        if expected == float("inf"):
            with pytest.raises(NoFeasiblePlan):
                greedy_cost_cents(R, route, stations)
            continue
        feasible += 1
        got, plan = greedy_cost_cents(R, route, stations)
        assert got == pytest.approx(expected, abs=1e-6), (route, stations)
        assert_invariants(plan, route, mpg=1.0)
    assert feasible > 100  # the test must actually exercise feasible, non-trivial cases


def test_adding_a_station_never_hurts():
    rng = random.Random(7)
    checked = 0
    for _ in range(300):
        R, route, stations = random_instance(rng)
        base = dp_min_cost(route, stations, R)
        if base == float("inf"):
            continue
        extra = (rng.randint(0, route), rng.randint(100, 400))
        more_cost, _ = greedy_cost_cents(R, route, stations + [extra])  # stays feasible
        assert more_cost <= base + 1e-6
        checked += 1
    assert checked > 50
