import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import FuelStation
from .serializers import RoutePlanRequestSerializer
from .services.geocoding import GeocodingError, resolve_us_location
from .services.optimizer import NoFuelPlan, find_corridor_candidates, plan_fuel_stops
from .services.routing import RoutingError, get_driving_route, simplify_line
from .services.stations import get_station_index, located_stations

METERS_PER_MILE = 1609.344


def _coverage():
    counts = FuelStation.objects.aggregate(
        total=Count("pk"),
        located=Count("pk", filter=Q(latitude__isnull=False, longitude__isnull=False)),
        city_centroid=Count("pk", filter=Q(geocode_status="city")),
    )
    total, located = counts["total"], counts["located"]
    return {
        "us_stations": total,
        "located_stations": located,
        "located_by_city_centroid": counts["city_centroid"],
        "unlocated_stations": total - located,
        "coverage_percent": round(100 * located / total, 1) if total else 0,
    }


@api_view(["GET"])
def health(request):
    return Response({"status": "ok", "fuel_stations": _coverage()})


def _public_location(location):
    return {key: value for key, value in location.items() if key != "from_cache"}


def _stop_payload(number, purchase, station):
    candidate = purchase["candidate"]
    return {
        "stop_number": number,
        "station_id": station.source_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "location_precision": "city_centroid" if station.geocode_status == "city" else "geocoded",
        "route_mile": round(candidate.route_mile, 1),
        "off_route_miles": round(candidate.off_route_miles, 1),
        "price_per_gallon_usd": round(candidate.price, 3),
        "fuel_on_arrival_gallons": round(purchase["fuel_on_arrival_gallons"], 2),
        "gallons_purchased": round(purchase["gallons"], 2),
        "cost_usd": round(purchase["cost"], 2),
    }


@api_view(["GET", "POST"])
def route_plan(request):
    """Plan a route with least-cost fuel stops. POST JSON or GET query parameters."""
    started = time.perf_counter()
    serializer = RoutePlanRequestSerializer(data=request.data if request.method == "POST" else request.query_params)
    if not serializer.is_valid():
        return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
    values = serializer.validated_data

    station_index = get_station_index()
    request_key = "|".join(str(values[name]) for name in sorted(values)).casefold()
    cache_key = "route-plan:" + sha256(f"{request_key}|{station_index.version}".encode()).hexdigest()
    if (cached := cache.get(cache_key)) is not None:
        cached["meta"] = {**cached["meta"], "cached": True, "external_api_calls": 0,
                          "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        return Response(cached)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            start_future = pool.submit(resolve_us_location, values["start_address"])
            finish_future = pool.submit(resolve_us_location, values["finish_address"])
            start, finish = start_future.result(), finish_future.result()
        route = get_driving_route(start, finish)
    except GeocodingError as exc:
        return Response({"error": "geocoding_failed", "detail": str(exc)}, status=422)
    except RoutingError as exc:
        return Response({"error": "routing_failed", "detail": str(exc)}, status=502)
    external_calls = sum(1 for result in (start, finish, route) if result.get("from_cache") is False)

    coordinates = route["geometry"]["coordinates"]
    distance_miles = route["distance_meters"] / METERS_PER_MILE
    fuel_plan = {"status": "available", "detail": ""}
    stops, total_gallons, total_cost, candidate_count = [], None, None, 0
    try:
        candidates = find_corridor_candidates(coordinates, distance_miles, station_index, values["corridor_miles"])
        candidate_count = len(candidates)
        plan = plan_fuel_stops(
            candidates,
            distance_miles,
            mpg=values["mpg"],
            max_range_miles=values["max_range_miles"],
            start_search_miles=values["corridor_miles"],
            stop_penalty_usd=values["stop_penalty_usd"],
        )
    except NoFuelPlan as exc:
        fuel_plan = {"status": "unavailable", "detail": str(exc)}
    else:
        station_ids = [int(station_index.ids[purchase["candidate"].index]) for purchase in plan["purchases"]]
        stations = located_stations().in_bulk(station_ids)
        stops = [
            _stop_payload(number, purchase, stations[station_id])
            for number, (purchase, station_id) in enumerate(zip(plan["purchases"], station_ids), start=1)
        ]
        total_gallons, total_cost = round(plan["total_gallons"], 2), round(plan["total_cost"], 2)

    tank_gallons = values["max_range_miles"] / values["mpg"]
    payload = {
        "summary": {
            "start": start["matched_address"],
            "finish": finish["matched_address"],
            "distance_miles": round(distance_miles, 1),
            "duration_hours": round(route["duration_seconds"] / 3600, 2),
            "fuel_stop_count": len(stops),
            "total_gallons": total_gallons,
            "total_fuel_cost_usd": total_cost,
        },
        "fuel_stops": stops,
        "route": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": simplify_line(coordinates)},
            "properties": {"start": _public_location(start), "finish": _public_location(finish)},
        },
        "map_url": request.build_absolute_uri("/") + "?" + urlencode(
            {"start": values["start_address"], "finish": values["finish_address"]}
        ),
        "fuel_plan": {
            **fuel_plan,
            "corridor_miles": values["corridor_miles"],
            "candidate_stations_in_corridor": candidate_count,
            "station_data": _coverage(),
        },
        "assumptions": {
            "mpg": values["mpg"],
            "max_range_miles": values["max_range_miles"],
            "tank_capacity_gallons": round(tank_gallons, 2),
            "stop_penalty_usd": values["stop_penalty_usd"],
            "starting_fuel": "Empty; the first fill is the cheapest station within the corridor "
                             "distance of the start, and the drive to it is charged at its price.",
            "optimization": "Minimises fuel cost plus stop_penalty_usd per stop (the penalty is not "
                            "included in total_fuel_cost_usd); set it to 0 for the pure cheapest plan.",
            "purchases": "Any amount can be bought; the plan arrives at the finish with an empty tank, "
                         "so gallons purchased = route miles / mpg.",
            "station_locations": "The fuel CSV has no coordinates; stations are placed at their city's "
                                 "Census internal point and must lie within the corridor of the route.",
            "price_date": "Not supplied in the source CSV.",
        },
        "meta": {
            "cached": False,
            "external_api_calls": external_calls,
            "elapsed_ms": 0,
        },
    }
    if fuel_plan["status"] == "available":
        cache.set(cache_key, payload, timeout=settings.ROUTE_PLAN_CACHE_SECONDS)
    payload["meta"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return Response(payload)
