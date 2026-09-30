# Fuel Route Planner

A Django + Django REST Framework service that plans cost-efficient fuel stops for long-distance driving routes across the contiguous United States.

The API accepts a start and finish location, obtains a driving route from OpenRouteService, finds nearby fuel stations from a local PostgreSQL dataset, and computes the minimum-cost fuel purchase plan.

## Highlights

* Django 6.1 + Django REST Framework 3.18
* PostgreSQL with no PostGIS dependency
* OpenRouteService for driving directions
* Redis route caching (in-process LocMem cache when `REDIS_URL` is unset)
* Deterministic greedy fuel optimization
* City/state and coordinate-based locations
* Bounding-box station candidate filtering
* Pure-Python route corridor projection
* Decimal-based fuel pricing
* Snapshot-safe CSV imports
* Structured API errors
* Request throttling
* Automated tests for algorithm, geometry, routing, imports, and API behavior
* Docker Compose configuration for local deployment

The system intentionally avoids unnecessary infrastructure such as Celery, PostGIS, route persistence, or background processing.

---

## Architecture

```text
Client
  |
  v
DRF API
  |
  +--> Resolve locations
  |       |
  |       +--> PostgreSQL Place lookup
  |
  +--> Obtain route
  |       |
  |       +--> Redis / LocMem cache
  |       |
  |       +--> OpenRouteService on cache miss
  |
  +--> Find candidate fuel stations
  |       |
  |       +--> PostgreSQL bounding-box query
  |       +--> Route corridor/grid filtering
  |
  +--> Optimize fuel purchases
  |       |
  |       +--> Pure Python greedy algorithm
  |
  +--> Load selected station metadata
  |
  v
JSON response
```

A normal city/state request requires at most three database queries:

1. Resolve start and finish locations.
2. Retrieve candidate fuel stations.
3. Retrieve metadata for selected stops.

Coordinate-based requests skip the location lookup.

---

## API

### Plan a Route

```http
POST /api/v1/routes/plan/
```

Example:

```json
{
  "start": "New York, NY",
  "finish": {
    "lat": 34.0522,
    "lng": -118.2437
  }
}
```

Locations can be supplied as either:

```json
"New York, NY"
```

or:

```json
{
  "lat": 40.7128,
  "lng": -74.0060
}
```

Free-text street addresses are intentionally not supported.

No geocoding API is called for location resolution.

#### Example Request

```bash
curl -X POST http://localhost:8000/api/v1/routes/plan/ \
  -H "Content-Type: application/json" \
  -d '{
    "start": "New York, NY",
    "finish": {
      "lat": 34.0522,
      "lng": -118.2437
    }
  }'
```

---

### Response

A successful response contains:

* route start and finish
* route distance and duration
* GeoJSON route geometry
* vehicle configuration
* fuel consumed
* fuel purchased
* fuel remaining
* total purchase cost
* selected fuel stops
* planning metadata

Example (**abbreviated**: `geometry.coordinates` and `stops` are truncated, so the single stop shown does not add up to the totals; a real cross-country response lists every stop):

```json
{
  "route": {
    "start": {
      "label": "New York, NY",
      "lat": 40.662712,
      "lng": -73.938677
    },
    "finish": {
      "label": "34.0522,-118.2437",
      "lat": 34.0522,
      "lng": -118.2437
    },
    "distance_miles": 2800.2,
    "duration_minutes": 2712.0,
    "geometry": {
      "type": "LineString",
      "coordinates": [
        [-73.93872, 40.6631],
        "..."
      ]
    }
  },
  "fuel": {
    "vehicle": {
      "range_miles": 500.0,
      "mpg": 10.0,
      "tank_gallons": "50.000"
    },
    "start_gallons": "50.000",
    "gallons_consumed": "280.018",
    "gallons_purchased": "230.018",
    "gallons_remaining_at_destination": "0.000",
    "total_fuel_cost": "695.20",
    "stops": [
      {
        "sequence": 1,
        "station": {
          "opis_id": 72445,
          "name": "SHEETZ #639",
          "address": "I-80 Exit 223",
          "city": "Youngstown",
          "state": "OH",
          "lat": 41.099095,
          "lng": -80.645902
        },
        "mile_marker": 400.8,
        "distance_from_route_miles": 3.8,
        "price_per_gallon": "3.0590",
        "gallons_on_arrival": "9.917",
        "gallons_purchased": "6.613",
        "gallons_after_purchase": "16.530",
        "cost": "20.23"
      }
      // ... 14 more stops omitted in this example
    ]
  },
  "meta": {
    "algorithm": "greedy-v1",
    "candidate_stations": 442,
    "route_cached": false,
    "corridor_miles": 10.0,
    "assumptions": ["..."]
  }
}
```

Each stop reports the station, its `mile_marker` along the route, `distance_from_route_miles` (how far the station's coordinates are from the route), the `price_per_gallon`, the fuel on arrival, the gallons purchased, the fuel after purchase, and the line `cost`. `total_fuel_cost` is the sum of the rounded line costs, so the receipt adds up.

Money and fuel quantities are returned as strings to avoid client-side floating-point rounding issues.

GeoJSON coordinates use the standard `[longitude, latitude]` order. Internal application coordinates use `lat` / `lng`.

---

## Fuel Model

The brief specifies:

* 500-mile vehicle range
* 10 MPG

This implies a 50-gallon tank:

```text
500 miles / 10 MPG = 50 gallons
```

The vehicle starts with a full tank.

Starting fuel is treated as already paid for and is therefore excluded from `total_fuel_cost`.

Only fuel purchased during the trip contributes to the reported cost.

The following invariant is maintained:

```text
start fuel + purchased fuel - consumed fuel = remaining fuel
```

#### Examples

| Distance | Fuel Consumed | Fuel Purchased | Fuel Remaining |     Fuel Cost |
| -------: | ------------: | -------------: | -------------: | ------------: |
|   300 mi |        30 gal |          0 gal |         20 gal |         $0.00 |
|   500 mi |        50 gal |          0 gal |          0 gal |         $0.00 |
|   750 mi |        75 gal |         25 gal |          0 gal | Purchase cost |

No reserve fuel is assumed.

---

## Fuel Optimization

The planner minimizes **total fuel purchase cost**.

It does not optimize:

* number of stops
* driving distance
* station detour distance
* stop duration

There is no stop fee or detour cost in the current model.

### Greedy Strategy

At each reachable station:

1. If the destination can be reached with the current fuel, finish.
2. Find the first reachable station with a strictly lower price.
3. If one exists, purchase only enough fuel to reach it.
4. Otherwise, if the destination is reachable after refueling, purchase only enough to finish.
5. Otherwise, fill the tank and continue toward the cheapest reachable station.
6. If neither the destination nor another station is reachable, the route is infeasible.

The starting location is not treated as a fuel station and no artificial fuel price is assigned to it.

### Why Greedy Works

Fuel purchased at a station should not be carried beyond a reachable cheaper station because the same fuel could be purchased there at a lower price.

Therefore:

* when a cheaper station is reachable, minimize the amount purchased now;
* when no cheaper station is reachable, the current station is at least as cheap as every reachable alternative, so filling the tank is optimal.

This exchange argument establishes the greedy-choice property for the current fuel model.

If the model later adds stop fees, detour costs, or other state-dependent costs, this algorithm would need to be replaced by a more general optimization approach.

The implementation is independently regression-tested against a discrete dynamic-programming oracle on randomized instances.

---

## Routing

OpenRouteService is used for driving directions.

```text
OpenRouteService
    |
    +-- driving-car profile
    +-- route geometry
    +-- distance
    +-- duration
```

A route is requested only when it is not already cached.

### Provider Calls

```text
Cache hit  -> 0 provider calls
Cache miss -> 1 provider call
```

Location resolution does not use an external geocoder.

### Retry Policy

Only transient failures are retried:

* connection errors
* timeouts
* HTTP 502
* HTTP 503
* HTTP 504

The default configuration allows one retry.

The following are not retried:

* HTTP 400
* HTTP 401
* HTTP 403
* HTTP 404
* HTTP 429
* other non-transient responses

Provider failures are translated into stable application-level API errors.

---

## Routing Provider

OpenRouteService was chosen because it provides, in a single request, a driving route with geometry, distance and duration, and it offers a free API key with published quotas. **A free key is required to run the API**: create one at https://openrouteservice.org/ and set `ORS_API_KEY`. The application does not depend on any specific quota number.

| Option | Decision |
| --- | --- |
| OpenRouteService | **Chosen.** Free key, driving profile, route geometry + distance + duration in one request, distances in miles. |
| OSRM public demo | No key, but no service guarantee and usage restrictions; not suitable as a dependency. |
| Google / Mapbox / HERE | Free tiers need billing setup; too much friction for this project. |
| GraphHopper / Valhalla public servers | Smaller quota or best-effort availability. |

A cold request is dominated by this external call (about 1.1 s in local measurements).

---

## Route Caching

Only the routing result is cached.

The fuel plan is intentionally recalculated because station data can change when the fuel dataset is imported.

The cache contains:

* route distance
* route duration
* simplified route geometry
* cumulative route mileage

Cache keys include a routing configuration version so changes to the routing request format cannot silently reuse incompatible cached data.

Default TTL:

```text
24 hours
```

Redis is used when `REDIS_URL` is configured.

Without Redis, Django's `LocMemCache` is used for local development and tests.

Invalid cached values are validated, discarded, and replaced with a fresh provider response.

---

## Station Data

The supplied dataset contains fuel station prices but does not contain usable latitude/longitude coordinates.

The importer enriches stations using US Census Gazetteer place data.

Stations are positioned at the centroid of their associated city.

This is an intentional approximation.

The raw Gazetteer can contain several places with the same normalized name, so `scripts/build_us_places.py` applies deterministic selection rules per `(state, name_key)`:

1. Incorporated place
2. Census-designated place
3. County subdivision
4. Largest land area
5. Lowest GEOID as the final tie-breaker

Land area is only a proxy for "the place the user meant", so an ambiguous name can resolve to a same-named place other than the intended one.

Approximately 97% of US stations in the supplied dataset receive coordinates. Stations that cannot be resolved remain in the database but are excluded from route candidate selection.

### Data Assumptions

The source file contains multiple price rows for some station IDs but does not provide price timestamps or a fuel-type field.

The importer therefore:

* uses OPIS ID as the station natural key;
* keeps the lowest price when multiple prices exist for one station;
* chooses a deterministic canonical station name;
* skips Canadian rows;
* preserves stations without coordinates;
* never invents price history.

These are dataset interpretations rather than claims about the underlying source data.

---

## Database Design

The application intentionally uses only two domain models.

### `Place`

Stores normalized US place data used to resolve:

```text
City, ST
```

Fields include:

* name
* normalized name
* state
* latitude
* longitude

Places are uniquely identified by:

```text
(state, name_key)
```

### `FuelStation`

Stores:

* OPIS ID
* station name
* address
* city
* state
* rack ID
* price
* latitude
* longitude
* import/update timestamp

The database uses PostgreSQL numeric types for prices.

A database constraint ensures latitude and longitude are either both present or both absent.

A partial latitude/longitude index supports candidate station queries.

No route or fuel-plan records are persisted because plans are transient.

---

## Fuel Data Import

The supplied CSV is treated as a complete snapshot.

```bash
python manage.py import_fuel_data data/fuel-prices.csv
```

The importer:

1. validates the input;
2. rejects suspiciously small snapshots;
3. parses and normalizes rows;
4. resolves station coordinates;
5. upserts stations by OPIS ID;
6. removes stations absent from the snapshot;
7. commits the entire operation atomically.

Suspicious snapshots are rejected before destructive changes begin.

`--force` can override size-based safety checks when intentionally importing a smaller dataset.

Example:

```bash
python manage.py import_fuel_data data/fuel-prices.csv --show-unmatched
```

Re-importing the same dataset is idempotent.

Canonical station names are selected deterministically so results do not depend on CSV row order.

---

## Geographic Candidate Selection

The application does not require PostGIS.

Candidate stations are first filtered using a PostgreSQL bounding-box query.

The remaining stations are matched against the route using a pure-Python spatial grid.

Every route segment is registered in the grid cells crossed by its corridor rather than only indexing segment endpoints.

For each candidate station, the planner calculates:

* nearest distance to the route;
* projected position along the route;
* route mile marker.

This avoids comparing every station against every route segment.

The implementation is tested against brute-force geometric calculations, including long route segments and corridor boundary cases.

---

## Route Geometry

The routing provider may return a large number of geometry points.

Two representations are used.

### Planning Geometry

A high-fidelity Douglas-Peucker simplification is used for route projection and station matching.

Tolerance:

```text
0.02 miles
```

This keeps geometric error significantly below the uncertainty introduced by city-centroid station coordinates.

Cumulative mileage is calculated from the full provider route before simplification so station mile markers are not distorted by geometry decimation.

### Response Geometry

A coarser:

```text
0.25-mile
```

simplification is used for the client response to keep JSON responses compact.

---

## API Errors

All application errors use a consistent envelope:

```json
{
  "error": {
    "code": "route_not_found",
    "message": "No driving route found between the locations.",
    "details": {}
  }
}
```

| Condition             | HTTP Status |
| --------------------- | ----------: |
| Invalid request       |         400 |
| Unknown location      |         422 |
| Outside service area  |         422 |
| No route              |         422 |
| No feasible fuel plan |         422 |
| Provider rate limit   |         503 |
| Provider error        |         502 |
| Provider timeout      |         504 |
| Database unavailable  |         503 |
| Application throttle  |         429 |
| Unexpected error      |         500 |

A `GET /api/v1/health/` endpoint performs an actual database connectivity check.

---

## Service Area

The service accepts coordinates within a contiguous-US bounding rectangle:

```text
Latitude:  24.4 to 49.6
Longitude: -125.0 to -66.9
```

This is a rectangle rather than a political boundary polygon.

As a result, some points in southern Canada, Mexico, or surrounding water can pass the initial coordinate check and fail later during routing or station selection.

Alaska and Hawaii are outside the current service area.

---

## Performance

Measured locally using PostgreSQL 18, real routing responses, and the supplied station dataset:

| Route                  | Distance | Stops |   Cold | Cached |
| ---------------------- | -------: | ----: | -----: | -----: |
| Chicago → Indianapolis |   184 mi |     0 | ~1.1 s |  ~8 ms |
| Boise → Nashville      | 1,929 mi |     8 | ~1.2 s | ~39 ms |
| Los Angeles → New York | 2,809 mi |    15 | ~1.3 s | ~73 ms |
| Seattle → Miami        | 3,329 mi |    17 | ~1.4 s | ~81 ms |

Cold latency is dominated by the external routing provider.

The optimizer itself runs in under a millisecond on the tested dataset.

### Database Query Budget

The planner maintains a bounded database query pattern.

#### City/State Request

```text
1. Location lookup
2. Candidate station query
3. Selected station metadata query
```

#### Coordinate Request

```text
1. Candidate station query
2. Selected station metadata query
```

A trip requiring no fuel stops does not perform the metadata query.

Query counts are covered by automated tests.

---

## Testing

167 tests across six files:

| Area | Tests | Covers |
| --- | ---: | --- |
| Optimizer | 22 | range boundaries, free starting tank, equal prices, clustered stations, unreachable gaps, fuel/cost invariants, randomized cross-check against an independent DP, adding a station never hurts |
| Routing client | 46 | success, retry table (what is and is not retried), timeouts, rate limits, malformed responses, credentials never logged, cache hits, corrupt cache entries, configurable timeouts/retries |
| API | 36 | request validation, response shape, every error code, query counts, provider call counts, cache behavior, throttling, timing with real route geometry |
| Settings | 29 | invalid configuration fails at startup, naming the variable |
| Importer | 18 | duplicates, Canadian rows, snapshot safety, idempotency, deterministic names, constraints, refusal of empty files |
| Geometry | 16 | distances, polyline decoding, simplification bounds, corridor boundaries, long segments, grid index vs brute force |

Run the test suite with (requires a reachable PostgreSQL; see Local Development):

```bash
pytest
```

Run linting and formatting checks with:

```bash
ruff check .
ruff format --check .
```

The test suite does not depend on live OpenRouteService requests. External routing behavior is mocked and a real provider response is available as a fixture.

---

## Configuration

Create a local environment file:

```bash
cp .env.example .env
```

Required:

```env
SECRET_KEY=
ORS_API_KEY=
DATABASE_URL=
```

Optional:

```env
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=
REDIS_URL=

VEHICLE_RANGE_MILES=500
VEHICLE_MPG=10
STATION_CORRIDOR_MILES=10

ROUTE_CACHE_TTL=86400

ORS_CONNECT_TIMEOUT=3
ORS_READ_TIMEOUT=15
ORS_MAX_RETRIES=1

PLAN_THROTTLE_RATE=30/min
```

### Configuration Reference

| Variable                 | Default                | Description                         |
| ------------------------ | ---------------------- | ----------------------------------- |
| `SECRET_KEY`             | Required in production | Django secret key                   |
| `DEBUG`                  | `False`                | Django debug mode                   |
| `ALLOWED_HOSTS`          | `localhost,127.0.0.1`  | Allowed hostnames (must not be empty when `DEBUG=False`) |
| `CSRF_TRUSTED_ORIGINS`   | Empty                  | Trusted origins; only relevant if a browser/session feature is added |
| `DATABASE_URL`           | Local PostgreSQL       | PostgreSQL connection URL           |
| `ORS_API_KEY`            | Required for routing   | OpenRouteService API key            |
| `REDIS_URL`              | Empty                  | Enables Redis-backed caching        |
| `VEHICLE_RANGE_MILES`    | `500`                  | Vehicle range                       |
| `VEHICLE_MPG`            | `10`                   | Vehicle fuel economy                |
| `STATION_CORRIDOR_MILES` | `10`                   | Maximum station distance from route |
| `ROUTE_CACHE_TTL`        | `86400`                | Route cache lifetime in seconds     |
| `ORS_CONNECT_TIMEOUT`    | `3`                    | ORS connection timeout              |
| `ORS_READ_TIMEOUT`       | `15`                   | ORS read timeout                    |
| `ORS_MAX_RETRIES`        | `1`                    | Maximum transient retries           |
| `PLAN_THROTTLE_RATE`     | `30/min`               | API request throttle                |

Configuration is validated during application startup.

Invalid or non-finite numeric values, invalid throttle rates, missing production secrets, and invalid host configuration cause startup to fail rather than allowing an invalid runtime configuration.

Never commit `.env` or API credentials.

---

## Local Development

### Requirements

* Python 3.12+
* PostgreSQL
* Optional Redis

### Setup

Create a virtual environment:

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\activate
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements/dev.txt
```

Create and configure `.env`:

```bash
cp .env.example .env
```

Create the PostgreSQL database named in `DATABASE_URL`:

```bash
createdb fuel_station
```

(or run `CREATE DATABASE fuel_station;` in `psql` / pgAdmin). The database user also needs permission to create databases, because `pytest` creates a temporary `test_...` database.

Run migrations:

```bash
python manage.py migrate
```

Load US places:

```bash
python manage.py load_places
```

Import fuel data:

```bash
python manage.py import_fuel_data data/fuel-prices.csv
```

Start the development server:

```bash
python manage.py runserver
```

The API is then available at:

```text
http://localhost:8000/api/v1/routes/plan/
```

---

## Docker

The repository includes Docker Compose configuration for:

* Django application
* PostgreSQL
* Redis

`docker-compose.yml` requires `SECRET_KEY` (it refuses to start without it). `ORS_API_KEY` is passed through to the app.

Start the stack:

```bash
export SECRET_KEY=change-me
export ORS_API_KEY=your-openrouteservice-key
docker compose up --build
```

PowerShell:

```powershell
$env:SECRET_KEY = "change-me"
$env:ORS_API_KEY = "your-openrouteservice-key"
docker compose up --build
```

Load reference data:

```bash
docker compose exec web python manage.py load_places
```

Import fuel data:

```bash
docker compose exec web python manage.py import_fuel_data data/fuel-prices.csv
```

Redis is used for shared route caching when multiple application workers are running.

**These Docker files have not been run.** Docker was not available on the development machine, so the application was verified against a local PostgreSQL 18 (and the in-process cache) instead. The Redis cache path is likewise untested against a live Redis server.

---

## Postman

A Postman collection is included:

```text
postman/fuel-route-planner.postman_collection.json
```

It covers:

* cross-country route planning
* cached requests
* short trips
* coordinate input
* unknown locations
* validation errors
* same start/finish
* service-area validation

For the cache scenario, run the same request twice and inspect:

```json
"route_cached": true
```

on the second request.

---

## Design Decisions

### Why PostgreSQL?

The application needs relational storage for:

* fuel stations
* place reference data
* constraints
* indexed candidate queries

PostgreSQL provides these capabilities without requiring additional spatial infrastructure for the current dataset size.

### Why No PostGIS?

The supplied dataset contains only thousands of stations.

Bounding-box filtering followed by the application-level route grid provides sufficient performance for the current workload without introducing a PostGIS/GDAL/GEOS dependency.

PostGIS remains a natural upgrade if the station dataset grows substantially.

### Why No Celery?

Route planning is synchronous and the expensive external operation is a single routing request.

Background jobs would add operational and architectural complexity without being required by the current workload.

### Why No Route Persistence?

Routes and fuel plans are transient outputs.

Only the routing provider response is cached because it is the expensive external operation.

### Why No Price History?

The source dataset contains no timestamps.

Persisting historical price records would imply information that the source does not provide.

---

## Known Limitations

The current implementation intentionally has the following limitations:

* station coordinates use city centroids;
* approximately 3% of stations cannot be geolocated from the supplied data;
* ambiguous city names use deterministic place-selection rules;
* duplicate station prices use the lowest observed value;
* free-text street addresses are unsupported;
* the service-area check uses a bounding rectangle rather than a political boundary polygon;
* station detour distance is not included in fuel cost;
* prices are imported data rather than live prices;
* the optimization objective does not include stop fees or detour costs, so the optimal plan can contain many small top-ups (for example 15 stops on a 2,800-mile trip);
* the starting tank is assumed to be full;
* no reserve fuel is required.

These limitations are explicit parts of the current problem model rather than hidden behavior.

---

## Future Extensions

If the requirements expand, possible next steps include:

* US boundary polygon validation
* external address geocoding
* highway-aware station coordinates
* timestamped fuel-price history
* live price ingestion
* reserve-fuel constraints
* stop and detour costs
* dynamic programming for expanded optimization constraints
* PostGIS for substantially larger station datasets
* structured metrics and distributed tracing
* self-hosted routing infrastructure

These are intentionally outside the current implementation scope.

---

## Project Structure

```text
fuel-route-optimizer/
│
├── config/                 # Django project configuration (settings, urls, wsgi)
├── planner/                # Route planning application
│   ├── models.py           # Place and FuelStation models
│   ├── names.py            # Place-name normalization (pure)
│   ├── locations.py        # "City, ST" / coordinate resolution
│   ├── routing.py          # OpenRouteService integration and route caching
│   ├── stations.py         # Candidate station selection along a route
│   ├── geo.py              # Geographic calculations and route grid index
│   ├── optimizer.py        # Fuel optimization algorithm (pure)
│   ├── pipeline.py         # End-to-end planning workflow
│   ├── serializers.py      # Request validation and response contract
│   ├── errors.py           # Application errors and error envelope
│   ├── importer.py         # Fuel and place data import logic
│   ├── views.py            # DRF endpoints
│   └── management/         # load_places, import_fuel_data commands
│
├── data/
│   ├── fuel-prices.csv
│   └── us_places.csv
│
├── scripts/
│   └── build_us_places.py
│
├── tests/                  # Automated test suite
├── postman/                # Postman collection
│
├── Dockerfile
├── docker-compose.yml
├── manage.py
├── pyproject.toml
├── requirements/
├── .env.example
└── README.md
```

---

## Security

* API credentials are loaded from environment variables.
* `.env` is excluded from version control.
* API keys are never included in API responses or logs.
* Provider credentials are not included in cache payloads.
* Invalid configuration fails during startup.
* Application throttling protects the routing-provider quota.
* Unexpected API errors do not expose stack traces to clients.

For deployment, HTTPS and the standard Django production security settings should be enabled at the infrastructure/application level.

---

## License

This project is provided for assessment purposes.

