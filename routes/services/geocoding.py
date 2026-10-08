import re
from hashlib import sha256

import requests
from django.conf import settings
from django.core.cache import cache

from .places import lookup_place, parse_city_state


class GeocodingError(Exception):
    pass


_COORDINATES = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
# Generous bounds covering the 50 states, DC and Puerto Rico.
_US_BOUNDS = ((17.5, 72.0), (-180.0, -64.0))


def resolve_us_location(text):
    """Resolve "lat,lon", "City, ST" (offline) or a street address (Census geocoder)."""
    normalized = " ".join(text.strip().split())

    if match := _COORDINATES.match(normalized):
        latitude, longitude = float(match.group(1)), float(match.group(2))
        (min_lat, max_lat), (min_lon, max_lon) = _US_BOUNDS
        if not (min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon):
            raise GeocodingError(f"Coordinates are outside the USA (expected 'latitude,longitude'): {normalized}")
        return {
            "latitude": latitude,
            "longitude": longitude,
            "matched_address": normalized,
            "source": "coordinates",
        }

    if city_state := parse_city_state(normalized):
        city, state = city_state
        if place := lookup_place(city, state):
            latitude, longitude, name = place
            return {
                "latitude": latitude,
                "longitude": longitude,
                "matched_address": f"{name}, {state}",
                "source": "census_gazetteer_city",
            }
        raise GeocodingError(f"No US city named '{city}' was found in {state}.")

    return geocode_us_address(normalized)


def geocode_us_address(address):
    normalized = " ".join(address.strip().split())
    cache_key = "census-geocode:" + sha256(normalized.casefold().encode("utf-8")).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        response = requests.get(
            settings.CENSUS_GEOCODER_URL,
            params={"address": normalized, "benchmark": settings.CENSUS_GEOCODER_BENCHMARK, "format": "json"},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeocodingError("The US Census address service could not be reached.") from exc

    matches = payload.get("result", {}).get("addressMatches", [])
    if not matches:
        raise GeocodingError(
            f"No US address match was found for: {normalized}. "
            "Use a street address, 'City, ST', or 'latitude,longitude'."
        )

    match = matches[0]
    coordinates = match.get("coordinates", {})
    try:
        result = {
            "longitude": float(coordinates["x"]),
            "latitude": float(coordinates["y"]),
            "matched_address": match.get("matchedAddress", normalized),
            "source": "census_geocoder",
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodingError("The address service returned an invalid coordinate.") from exc

    cache.set(cache_key, result, timeout=60 * 60 * 24 * 30)
    return {**result, "from_cache": False}
