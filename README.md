# Fuel Route Planner

`POST /api/v1/routes/plan/` takes a start and finish in the contiguous US, makes **one**
OpenRouteService call for the driving route, picks the cost-optimal fuel stops from a local
PostgreSQL table of truck-stop prices, and returns the route geometry, each stop, and the money
spent on fuel. Django 6.1 + Django REST Framework 3.18, PostgreSQL, no PostGIS, no Celery.

```
client -> DRF view -> resolve locations (1 query, no API call)
                   -> route (cache | 1 ORS call) -> candidate stations (1 bbox query + corridor projection)
                   -> optimizer (pure Python) -> selected-stop metadata (1 query) -> JSON
```

## Assumptions (read these first)

The brief gives a 500-mile range and 10 MPG and nothing else. These are the interpretations I made:

1. **Tank = 50 gallons** (500 mi ÷ 10 MPG). Inferred from the brief, not invented.
2. **The vehicle starts with a full 50-gallon tank; that fuel is already paid for and is *not*
   in `total_fuel_cost`.** No price is invented for it. Only fuel bought at stations is charged.
   A trip of 500 miles or less therefore returns `stops: []` and `total_fuel_cost: "0.00"`.
3. Four quantities are never conflated: **fuel consumed** (`distance / mpg`), **fuel purchased**,
   **fuel remaining** and **purchase cost**. Invariant: `start + purchased - consumed = remaining >= 0`.
   - 300 mi: consumed 30 gal, purchased 0, cost $0, remaining 20 gal.
   - 750 mi: consumed 75 gal, start 50, purchased 25 gal, only those 25 gal are charged.
4. **The only objective is total purchase cost.** The plan does not promise the fewest stops or
   the least detour. Where several plans cost the same, fixed rules pick one deterministically
   (below); those are tie-breakers, not a secondary goal. Because there is no per-stop fee in the
   model, the optimum often contains several small top-ups (e.g. "buy 1.1 gal here because the
   next station is cheaper"). That is correct for the stated objective; a stop fee would change it.
5. The vehicle may arrive with exactly zero fuel (no reserve). Lower `VEHICLE_RANGE_MILES` to emulate one.
6. Every price in the file is treated as the price of the vehicle's fuel (there is no fuel-type column).
7. **Duplicate price rows per station are a data interpretation, not a fact.** 597 station IDs
   appear with several different prices and the file has no timestamp or explanation. I read them
   as alternative price points for the same station and keep the **lowest**. If they are really
   different products or tiers, this understates cost. The rule lives in one function
   (`importer.choose_price`).
8. **Stations are located at their city centroid** (see Data). Station positions therefore carry
   an error of a few miles, so a station counts as "on the route" when within **10 miles**
   (`STATION_CORRIDOR_MILES`), and the response reports `distance_from_route_miles` per stop.
   Detour distance to reach a station is not charged.

## API

Locations are either `"City, ST"` (resolved locally, no external call) or `{"lat": .., "lng": ..}`.
Free-text street addresses are **not** supported (no geocoding call is made); they return 422.

```bash
curl -X POST http://localhost:8000/api/v1/routes/plan/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": {"lat": 34.0522, "lng": -118.2437}}'
```

Response 200 (**abbreviated**: geometry coordinates and stops are truncated with `...`, so the
single stop shown does not add up to the totals; a real cross-country response lists every stop):

```json
{
  "route": {
    "start":  {"label": "New York, NY", "lat": 40.662712, "lng": -73.938677},
    "finish": {"label": "34.0522,-118.2437", "lat": 34.0522, "lng": -118.2437},
    "distance_miles": 2800.2,
    "duration_minutes": 2712.0,
    "geometry": {"type": "LineString", "coordinates": [[-73.93872, 40.6631], "..."]}
  },
  "fuel": {
    "vehicle": {"range_miles": 500.0, "mpg": 10.0, "tank_gallons": "50.000"},
    "start_gallons": "50.000",
    "gallons_consumed": "280.018",
    "gallons_purchased": "230.018",
    "gallons_remaining_at_destination": "0.000",
    "total_fuel_cost": "695.11",
    "stops": [
      {
        "sequence": 1,
        "station": {"opis_id": 72445, "name": "SHEETZ #639", "address": "I-80 Exit 223",
                    "city": "Youngstown", "state": "OH", "lat": 41.099095, "lng": -80.645902},
        "mile_marker": 398.4,
        "distance_from_route_miles": 3.8,
        "price_per_gallon": "3.0590",
        "gallons_on_arrival": "10.158",
        "gallons_purchased": "6.457",
        "gallons_after_purchase": "16.615",
        "cost": "19.75"
      }
    ]
  },
  "meta": {"algorithm": "greedy-v1", "candidate_stations": 442, "route_cached": false,
           "corridor_miles": 10.0, "assumptions": ["..."]}
}
```

Coordinate conventions: everywhere internally and in JSON points are `lat`/`lng`; **GeoJSON
`[lng, lat]` appears only inside `route.geometry.coordinates`**, and the ORS client converts at its boundary.
Money and gallons are strings (no client float drift). Money is rounded to cents half-up **per stop line**, and
`total_fuel_cost` is the sum of those rounded lines so the receipt adds up. Rounding happens only in the
serializer/pipeline output; the optimizer works with unrounded values.

Errors always use `{"error": {"code", "message", "details"}}`:

| Situation | HTTP | code |
|---|---|---|
| Malformed JSON, missing/blank/invalid field, bad lat/lng, start == finish | 400 | `validation_error` |
| Unknown city / not `City, ST` | 422 | `location_not_found` |
| Outside the contiguous-US rectangle | 422 | `location_outside_service_area` |
| Provider says no route / point not routable | 422 | `route_not_found` |
| A gap longer than the range with no station (details: `stuck_at_mile`, `max_reachable_mile`, `gap_miles`) | 422 | `no_feasible_fuel_plan` |
| Provider rate limited (429) | 503 + `Retry-After` | `routing_rate_limited` |
| Provider 5xx / malformed body / bad credentials / not configured | 502 | `routing_provider_error` |
| Provider timeout | 504 | `routing_timeout` |
| Database down | 503 | `database_unavailable` |
| Our own throttle (`PLAN_THROTTLE_RATE`, default `30/min`) | 429 | `throttled` |
| Unexpected | 500 | `internal_error` (no stack trace in the body) |

400 means the request is malformed; 422 means it is understood but cannot be satisfied; 404 is only for unknown URLs.
`GET /api/v1/health/` checks the database.

The location check for coordinates is a **rectangle** (lat 24.4-49.6, lng -125.0 to -66.9), not a border
polygon. It rejects Europe or Mexico City but accepts some points in southern Canada/Mexico/the ocean
inside the rectangle; those fail later as `route_not_found` or find no stations.

## Routing provider: OpenRouteService

| Option | Verdict |
|---|---|
| **OpenRouteService** | **Chosen.** Free key, published free-tier quotas, driving-car profile, one request returns polyline + distance + duration, distance in miles via `units=mi`. |
| OSRM public demo | Keyless, but "no excessive use, no SLA, can be blocked"; not something to depend on. |
| Google / Mapbox / HERE | Free tiers need billing setup; too much friction for a take-home. |
| GraphHopper / Valhalla public | Smaller quota / best-effort. |

**Requires a free API key** (https://openrouteservice.org/): set `ORS_API_KEY`. Observed on the
live account (2026-09-30): responses carried `X-Ratelimit-Limit: 200`; check your own dashboard,
nothing in the code depends on the number. A cross-country request returns an ~80 KB response in ~1.1 s.

**External calls per request:** 1 on a cache miss, **0 on a hit**. Zero geocoding calls (locations resolve locally).
Retry policy, used identically in code and tests: one retry, only on connection errors, timeouts and HTTP
502/503/504. Never retried: 400, 401, 403, 404, 429 (and any other status such as 500).

## Data and database design

`data/fuel-prices.csv` has 8,151 rows, no coordinates, no timestamps. Findings: 620 Canadian rows
(skipped), 26 exact duplicates, 597 stations with several prices, 227 with name variants, addresses are
highway exits (`I-44, EXIT 283 & US-69`) that street geocoders can't place.

Two models, each justified:

- **`FuelStation`**: one row per OPIS id (natural key, `unique`). `price` is `numeric(6,4)` (never float),
  nullable lat/lng with a `CHECK` that both or neither are set, `CHECK 0 < price < 20`, a partial B-tree index on
  `(latitude, longitude)`. `updated_at` is the *local record update timestamp* (when the importer last changed the
  row), not a price date. A price-history table was rejected: the file has no timestamps, so history is unrepresentable.
- **`Place`**: reference geodata (US Census Gazetteer 2023: places + county subdivisions, public domain) used for
  `"City, ST"` lookup and for geocoding stations at import time. `unique(state, name_key)`.

No Route/RouteRequest tables: routes are transient (cached), not domain data.

**Geocoding approach.** Stations are placed at the centroid of their city from the Gazetteer: no API calls, ever.
The raw Gazetteer does *not* have unique names (e.g. a city and a CDP both called "Cottonwood, AZ"), so
`scripts/build_us_places.py` builds `data/us_places.csv` with a deterministic rule per `(state, name_key)`:
incorporated place over CDP over county subdivision, then largest land area, then lowest GEOID. **This is an
intentional approximation** (land area is a proxy for "the place you meant"), so an ambiguous name can resolve to
the wrong same-named place. Consolidated governments get short aliases (`Lexington-Fayette` -> `Lexington`).
Measured on this file: **6,410 of 6,626 US stations (96.7%) get coordinates**; the rest are stored with null
coordinates and never become candidates. (List them with `import_fuel_data --show-unmatched`.)
A geocoding fallback for those ~3% was considered and **not built**.

**Import semantics.** The CSV is a *complete snapshot*: `import_fuel_data` upserts by OPIS id and **deletes
stations absent from the file**, all in one transaction. Because that is destructive, a suspicious file is refused
**before** the transaction starts: no usable rows (always refused, even with `--force`), fewer than `--min-rows`
(default 1000 stations / 10000 places), or fewer than half the rows already stored. `--force` overrides the size
checks only. `load_places` has the same guards. Re-importing the same file is a no-op. The canonical name
is the most frequent variant (ties -> lexicographically smallest), so results don't depend on row order.

## Algorithm

Model: route length `D`; stations at positions `s_i` (miles along the route) with prices `p_i`; tank `C = 50` gal
(range `R = 500` mi). The start is **not** a station: it is an initial state (full free tank) and nothing is ever
bought there. Minimize the money spent at stations.

Terms: *current station*, *current fuel* (miles of range), *reachable stations* (after the current one, within one
tank; from the start, within the free fuel), *first cheaper station* (nearest by route position with a strictly
lower price than the current station), *cheapest reachable station* (lowest price, ties -> farthest along the
route, then lowest id). Loop:

1. Destination reachable on the current fuel: stop.
2. At the start: drive to the cheapest reachable station (no purchase).
3. At a station: if a first cheaper station is reachable, buy just enough to reach it. Otherwise, if the
   destination is within a tank, buy just enough to finish. Otherwise fill up and go to the cheapest reachable
   station. No reachable station and destination out of range: infeasible (422).

Why it is optimal (exchange argument): fuel bought at a station is only ever used up to the next cheaper station
(anything used past it could have been bought there for less); so either the next cheaper station is within a tank
(buy the minimum to get there; more is dominated, less is infeasible) or none is (every unit up to capacity is at
least as cheap as anything reachable, so fill up). A single tank size and a constant per-station price give this
greedy-choice property. **If a per-stop fee or detour cost were added, greedy would no longer be optimal** and it
would need a DP over (station, fuel).

Verification: hand-built cases (range boundary 500 / 500.001, reachability, clusters, equal prices, order
independence), invariants, and a cross-check against an independently written brute-force DP on 400 random
**discretized** instances (integer miles and cents), plus a property test that adding a station never raises the
optimal cost or makes a feasible trip infeasible. The DP is a regression check on many shapes, not a proof about the
continuous problem; the exchange argument is the justification.

Complexity: sort `O(n log n)` plus `O(k*w)` for `k` stops and `w` stations per 500-mile window; the measured
optimizer time is under a millisecond.

All optimizer arithmetic is unrounded (float miles for feasibility with a 1e-6 tolerance, `Decimal` for money);
rounding happens only when building the response.

## Performance

Measured locally (Windows laptop, PostgreSQL 18, `runserver`), real ORS, real data:

| Route | Miles | Stops | Cold (ORS call) | Warm (route cached) |
|---|---|---|---|---|
| Chicago -> Indianapolis | 184 | 0 | ~1.1 s | 8 ms |
| Boise -> Nashville | 1,929 | 8 | ~1.2 s | 39 ms |
| Los Angeles -> New York | 2,809 | 15 | ~1.3 s | 73 ms |
| Seattle -> Miami | 3,329 | 17 | ~1.4 s | 81 ms |

Cold latency is dominated by the routing provider (~1.1 s). Warm time is candidate selection (up to ~70 ms on
cross-country routes) and optimization is ~0 ms. Per-stage timings are logged on every request (`route_plan ok ...`).

- **Queries: exactly 3** on the `"City, ST"` path: 1 `Place` query for both inputs, 1 bounding-box candidate
  query, 1 metadata query for the selected stops. Coordinates skip the Place query; a trip needing no stops skips
  the metadata query. Tests pin these counts.
- **Candidate selection:** SQL bounding box (`values_list`, no model instances), then a grid index over the route.
  Every route segment is registered in every grid cell its 10-mile corridor touches (sampled along the whole
  segment, not just at vertices), so a station beside the middle of a long straight segment is found. Lookup is a
  dict access plus a few exact point-to-segment tests. Tested against a brute-force nearest-segment on random routes.
- **`EXPLAIN ANALYZE`** on the real table: a cross-country bounding box (4,697 of 6,626 rows) does a seq scan in
  ~1.8 ms, which is the right plan at this size; a narrow box uses `station_lat_lng_idx` (Bitmap Index Scan,
  0.17 ms). The index is there for correctness at larger scale, not claimed as a win here.
- **Route geometry.** The optimizer and station matching use the provider's route at full fidelity within a stated,
  measured bound, not a thinned copy: the 21k-vertex provider polyline is simplified with Douglas-Peucker to a
  **0.02-mile (~32 m) tolerance** (no dropped vertex is farther than that from the kept path), which is ~250x smaller
  than the city-centroid uncertainty of the stations. **Mile markers are computed from the full path's cumulative
  distance** at each kept vertex, so simplification cannot shorten the route (an earlier version thinned vertices by
  0.5 mi and scaled the shorter length, which drifted mile markers by up to ~5 mi and off-route distances by 0.27 mi).
  Measured on the real cross-country route against the full-resolution polyline: off-route distance differs by
  <= 0.05 mi for every station; for stations near the road, mile markers differ by <= 0.15 mi. For a station several
  miles off a sharp bend, "the nearest point on the road" is ambiguous between near-equidistant stretches, so its
  mile marker can differ by more than that from the full-resolution answer; its distance is still within tolerance,
  and this is inherent to projecting a point, not to simplification. A separate, coarser simplification (0.25 mi) is
  used only for the geometry returned to the client.
- Response: ~21 KB (geometry simplified with Ramer-Douglas-Peucker to a 0.25-mile tolerance, ~700 points
  coast to coast) instead of the provider's payload.
- **PostGIS was considered and rejected**: it adds GDAL/GEOS installation pain and a different DB image for 6.6k
  points. Upgrade path if the dataset grows 100x: GiST index + `ST_DWithin` + `ST_LineLocatePoint`.

## Caching

Only the **provider route** is cached (encoded simplified polyline + full-path mileage + distance + duration), never
the fuel plan
(it depends on station data that changes on import, and recomputing takes milliseconds).
Key: `route:{ROUTING_CACHE_VERSION}:sha1(rounded start|finish)`; the version string encodes provider, profile, request
options and payload format (`ors-driving-car-mi-rdp0.02-v2`), so changing any of them can't read stale entries.
Coordinates are rounded to 4 decimals (~11 m). TTL 24 h (`ROUTE_CACHE_TTL`). A failing cache backend degrades to the
provider. **Cached values are validated on read** (keys, types, decodable polyline, mileage list of the right length and
monotonic); a malformed or stale entry is logged (no credentials), deleted, treated as a miss, and replaced by the
fresh provider result. A fresh route is served through the same encode/decode round trip as a cached one, so a cache hit
returns exactly the same plan as the request that filled it. Backend: Django's built-in `RedisCache` when `REDIS_URL` is set, otherwise in-process `LocMemCache`.
Redis matters only with multiple workers (LocMem is per-process, so each worker would spend provider quota separately)
and it survives deploys; nothing else uses it. `meta.route_cached` shows hits.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements/dev.txt
cp .env.example .env                                     # set DATABASE_URL, SECRET_KEY, ORS_API_KEY
python manage.py migrate
python manage.py load_places                             # data/us_places.csv (45k places)
python manage.py import_fuel_data data/fuel-prices.csv   # add --show-unmatched to list ungeocoded cities
python manage.py runserver
```

`data/us_places.csv` is committed; `python scripts/build_us_places.py` regenerates it from the Census files.

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | required unless `DEBUG=True` | Django secret |
| `DEBUG` | `False` | |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | comma separated |
| `DATABASE_URL` | local postgres | `postgres://user:pass@host:5432/db` |
| `ORS_API_KEY` | none | OpenRouteService key (**required** for routing) |
| `REDIS_URL` | empty | enables Redis cache |
| `VEHICLE_RANGE_MILES` / `VEHICLE_MPG` | 500 / 10 | vehicle model |
| `STATION_CORRIDOR_MILES` | 10 | how far off-route a station may be |
| `ROUTE_CACHE_TTL` | 86400 | seconds |
| `PLAN_THROTTLE_RATE` | `30/min` | protects the provider quota |

## Tests

```bash
pytest          # needs a PostgreSQL the DATABASE_URL user can create a test database on; no network
ruff check . && ruff format --check .
```

130+ tests: optimizer rules and DP cross-check (also mutation-checked: deliberately breaking the optimizer makes the
suite fail), geometry/grid index and simplification bounds, routing client (retry table, cache incl. corrupt entries,
malformed responses, key never logged), importer (snapshot semantics, refusal of empty/tiny files, idempotency, constraints), API (shape, errors, query
counts, provider call count, cache hit, throttle) and a timing guard using the real ORS geometry with 4,000 synthetic
stations. The ORS network is always mocked (`responses`); a real ORS response is committed as a fixture.

## Docker

```bash
export SECRET_KEY=change-me ORS_API_KEY=your-key
docker compose up --build
docker compose exec web python manage.py load_places
docker compose exec web python manage.py import_fuel_data data/fuel-prices.csv
```

`web` + PostgreSQL + Redis (Redis is justified by the multi-worker cache above). **These Docker files were written but
not run** (Docker is not installed on the development machine); the same code was verified against a local PostgreSQL 18.

## Postman

Import `postman/fuel-route-planner.postman_collection.json` (base URL variable defaults to `http://localhost:8000`).
It covers a cross-country plan (run it twice to see `route_cached: true`), a short trip ($0, no stops), coordinate
input, unknown city, same start/finish, a missing field and an out-of-area point, each with assertions.

## Known limitations

- Station coordinates are city centroids (a few miles of error; ~3% of stations have none), ambiguous city names
  use an area-based heuristic, and duplicate price rows use "lowest" (assumptions 7-8).
- Cost-only objective: many small top-ups, no stop fee, no reserve, no detour cost, no live prices.
- Free-text addresses are not supported; only `City, ST` and coordinates.
- The US check is a rectangle, not a polygon; Alaska/Hawaii/Canada stations aren't in scope.
- Cold requests depend on the routing provider's latency and quota.

## With more time

Border polygon and address geocoding (fallback for unmatched cities); snapping stations to actual highway exits
(OSM) instead of city centroids; price freshness/timestamps and history; DP variant with stop fees and reserve fuel;
self-hosted Valhalla/OSRM; PostGIS if the dataset grows; structured metrics/tracing; deploy on Cloud Run + Cloud SQL +
Memorystore.
