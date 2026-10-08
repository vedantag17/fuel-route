from hashlib import sha256

import requests
import shapely
from django.conf import settings
from django.core.cache import cache


class RoutingError(Exception):
    pass


def get_driving_route(start, finish):
    points = (start["longitude"], start["latitude"], finish["longitude"], finish["latitude"])
    cache_key = "osrm-route:" + sha256(",".join(f"{value:.5f}" for value in points).encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    coordinates = (
        f"{start['longitude']:.6f},{start['latitude']:.6f};"
        f"{finish['longitude']:.6f},{finish['latitude']:.6f}"
    )
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coordinates}"
    try:
        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError("The routing service could not be reached.") from exc

    routes = payload.get("routes", [])
    if payload.get("code") != "Ok" or not routes:
        raise RoutingError("No driving route was found between those locations.")

    selected = routes[0]
    geometry = selected.get("geometry")
    coordinates = geometry.get("coordinates", []) if geometry else []
    if geometry.get("type") != "LineString" or len(coordinates) < 2:
        raise RoutingError("The routing service returned an invalid route geometry.")

    result = {
        "geometry": geometry,
        "distance_meters": float(selected["distance"]),
        "duration_seconds": float(selected.get("duration", 0)),
    }
    cache.set(cache_key, result, timeout=60 * 60)
    return {**result, "from_cache": False}


def simplify_line(coordinates, tolerance_degrees=0.0005):
    """Shrink a GeoJSON LineString for responses; ~50 m tolerance is invisible on a map."""
    simplified = shapely.simplify(shapely.linestrings(coordinates), tolerance_degrees, preserve_topology=False)
    return [[round(x, 5), round(y, 5)] for x, y in shapely.get_coordinates(simplified)]
