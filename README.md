# Fuel Route Planner API

A Django REST API that takes a start and finish location in the USA and returns:

- the driving route as GeoJSON, with a map link,
- where to buy fuel along the way, and how much, chosen for low cost from the supplied fuel-price CSV,
- the total fuel cost for a vehicle with a 500-mile range and 10 mpg.

A typical uncached backend route request makes **one external API call**: the OSRM route. City names and coordinates are resolved from a bundled Census file, and repeated requests are cached. The browser demo separately loads its basemap from OpenFreeMap.

## Quick start

```sh
python -m venv .venv && source .venv/bin/activate   # or: conda activate Projects
pip install -r requirements.txt
python manage.py migrate
python manage.py import_fuel_prices        # ~2 s, no network needed
python manage.py runserver
```

- Map demo: <http://127.0.0.1:8000/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Route planner: `POST /api/v1/route-plan/` (or `GET` with the same query parameters)
- Data status: `GET /api/v1/health/`
- Postman collection: [`postman_collection.json`](postman_collection.json)
- Tests: `python manage.py test routes`

## Request

```json
{
  "start_address": "New York, NY",
  "finish_address": "Los Angeles, CA"
}
```

Locations can be `"City, ST"` (or a full state name), a street address, or `"latitude,longitude"`. Optional fields:

| Field | Default | Meaning |
|---|---|---|
| `mpg` | `10` | Fuel economy |
| `max_range_miles` | `500` | Range on a full tank (max 500) |
| `corridor_miles` | `15` | How far from the route a station may be |
| `stop_penalty_usd` | `5` | Cost of one extra stop, so the plan does not stop to save a few cents. Use `0` for the lowest possible fuel cost. |

## Response (abridged)

```json
{
  "summary": {
    "start": "New York, NY", "finish": "Los Angeles, CA",
    "distance_miles": 2810.4, "duration_hours": 50.31,
    "fuel_stop_count": 7, "total_gallons": 281.02, "total_fuel_cost_usd": 860.11
  },
  "fuel_stops": [
    {"stop_number": 1, "station_id": "62790", "name": "7-ELEVEN #40084", "address": "US-46/US-1/US-9",
     "city": "Palisades Park", "state": "NJ", "latitude": 40.847017, "longitude": -73.997061,
     "location_precision": "city_centroid", "route_mile": 10.0, "off_route_miles": 8.3,
     "price_per_gallon_usd": 3.099, "fuel_on_arrival_gallons": 0.0,
     "gallons_purchased": 39.6, "cost_usd": 122.72}
  ],
  "route": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-73.94, 40.66], "..."]}},
  "map_url": "http://127.0.0.1:8000/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA",
  "fuel_plan": {"status": "available", "candidate_stations_in_corridor": 515, "station_data": {"...": "..."}},
  "assumptions": {"...": "..."},
  "meta": {"cached": false, "external_api_calls": 1, "elapsed_ms": 1667}
}
```

`route` is a GeoJSON Feature that you can paste into <https://geojson.io>, or open `map_url` for an interactive map with the route, stops and a stop table.

## How it works

1. **Locate the start and finish.** `"lat,lon"` inputs are used directly. `"City, ST"` inputs are looked up in `data/us_places.csv`, built from Census Gazetteer internal points. Only street addresses call the US Census geocoder. The two lookups run in parallel and are cached for 30 days.
2. **Route.** One OSRM `/route` call returns the full route geometry, cached for one hour.
3. **Corridor search.** Station coordinates and prices stay in memory as NumPy arrays. A bounding-box filter removes most stations. The rest are projected to metres around the route and measured against it in one vectorized Shapely call, giving each station's distance from the route and its position (route mile) along it.
4. **Optimize.** A dynamic program walks the corridor stations in route order. It tracks the cheapest cost of reaching each station with every possible fuel level, in quarter-mile steps up to the tank size. At each station the vehicle either passes, or stops and buys any amount. The plan minimizes fuel cost plus `stop_penalty_usd` per stop. With a penalty of 0 it gives the exact minimum fuel cost, including filling up at a cheap station to drive past expensive ones. Total gallons always equal route miles divided by mpg.
5. The response is cached for one hour, so repeat requests make zero external calls and take a few milliseconds.

Measured timings: 1.7–2.6 s for an uncached cross-country route, almost all of it the OSRM call. Station search and optimization take about 0.3 s. Cached requests take under 10 ms.

## Fuel data

`import_fuel_prices` loads `data/fuel-prices-for-be-assessment.csv`:

- 8,151 rows become **6,626 unique US stations**. 620 Canadian rows are skipped. The same OPIS ID appears with several names or prices in 905 rows, and the lowest price is kept for each ID.
- The CSV has no coordinates, and its addresses are highway exits ("I-44, EXIT 283 & US-69") that street geocoders can't match. Each station is placed at its **city's Census internal point**, which locates **6,428 stations (97%)** offline. Spelling variants such as St./Saint, Mc Calla/McCalla and La Place/LaPlace are handled.
- If an enriched CSV includes `Latitude`/`Longitude` columns, those are used instead.
- Optional: `python manage.py geocode_fuel_stations` sends the remaining ~200 unlocated rows to the Census batch geocoder in one request.

Because stations sit at city centres, map markers show the town, not the exact exit. The 15-mile corridor allows for that.

Rebuild the places file from newer Census data with:

```sh
python manage.py build_places_index 2025_Gaz_place_national.txt 2025_Gaz_cousubs_national.txt
```

## Assumptions

- The vehicle starts empty. It first fills at the cheapest station within `corridor_miles` of the start, and the miles driven to that station are charged at its price.
- Any amount of fuel can be bought, and the plan arrives with an empty tank.
- Detours from the route to a station are not added to the fuel used. `off_route_miles` is reported for each stop.
- The CSV has no price date, so costs are estimates from that snapshot.

## Configuration

Environment variables (see `.env.example`): `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, and `OSRM_BASE_URL`, which defaults to the public demo server (best-effort, so keep request volume modest). The browser demo uses OpenFreeMap's public basemap, with its OpenFreeMap, OpenMapTiles, and OpenStreetMap attribution shown on the map. The basemap provider is separate from OSRM, which supplies driving routes.
