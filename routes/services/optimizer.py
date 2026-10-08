"""Corridor search and least-cost refuelling plan along a single route.

Stations are projected onto the route once (vectorised with Shapely/pyproj). A
dynamic program then walks the stations in route order, tracking the cheapest way
to be at each station with every possible amount of fuel (in quarter-mile units).
At each station the vehicle may pass, or stop and buy any amount; a stop adds an
optional fixed penalty so the plan does not detour for a few cents. With a zero
penalty the result is the exact minimum fuel cost for the route.
"""

from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import CRS, Transformer

METERS_PER_MILE = 1609.344
MILES_PER_DEGREE_LATITUDE = 69.0
FUEL_UNITS_PER_MILE = 4


class NoFuelPlan(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    index: int  # Position in the StationIndex arrays.
    route_mile: float
    off_route_miles: float
    price: float


def find_corridor_candidates(route_coordinates, route_distance_miles, station_index, corridor_miles):
    """Return stations within ``corridor_miles`` of the route, sorted by route mile."""
    if len(station_index) == 0:
        return []
    route = np.asarray(route_coordinates, dtype=np.float64)
    lons, lats = route[:, 0], route[:, 1]

    # Cheap bounding-box prefilter in degrees before any projection work.
    lat_margin = corridor_miles / MILES_PER_DEGREE_LATITUDE
    max_abs_lat = min(float(np.abs(lats).max()) + lat_margin, 89.0)
    lon_margin = lat_margin / np.cos(np.radians(max_abs_lat))
    in_box = (
        (station_index.longitudes >= lons.min() - lon_margin)
        & (station_index.longitudes <= lons.max() + lon_margin)
        & (station_index.latitudes >= lats.min() - lat_margin)
        & (station_index.latitudes <= lats.max() + lat_margin)
    )
    box_indices = np.flatnonzero(in_box)
    if box_indices.size == 0:
        return []

    # Azimuthal equidistant projection centred on the route keeps distances in metres
    # accurate to a few percent even for coast-to-coast routes.
    local_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lats.mean()} +lon_0={lons.mean()} +datum=WGS84 +units=m +no_defs"
    )
    transformer = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    route_x, route_y = transformer.transform(lons, lats)
    # OSRM returns tens of thousands of vertices for long routes; distance queries scale
    # with vertex count, so simplify to 50 m first (negligible next to a 15-mile corridor).
    line = shapely.simplify(shapely.linestrings(route_x, route_y), 50.0, preserve_topology=False)
    if line.length == 0:
        raise NoFuelPlan("The route geometry has no measurable length.")
    station_x, station_y = transformer.transform(
        station_index.longitudes[box_indices], station_index.latitudes[box_indices]
    )
    points = shapely.points(station_x, station_y)

    off_route_miles = shapely.distance(line, points) / METERS_PER_MILE
    near = off_route_miles <= corridor_miles
    route_miles = shapely.line_locate_point(line, points[near]) / line.length * route_distance_miles

    candidates = [
        Candidate(index=int(index), route_mile=float(mile), off_route_miles=float(off), price=float(price))
        for index, mile, off, price in zip(
            box_indices[near], route_miles, off_route_miles[near], station_index.prices[box_indices[near]]
        )
    ]
    candidates.sort(key=lambda candidate: (candidate.route_mile, candidate.price, candidate.off_route_miles))
    return candidates


def _cheapest_per_position(candidates):
    """Many stations share a city centroid; keep only the cheapest at each route position."""
    kept = []
    for candidate in candidates:
        if kept and abs(kept[-1].route_mile - candidate.route_mile) < 0.01:
            continue  # Input is sorted by (route_mile, price), so the first one is cheapest.
        kept.append(candidate)
    return kept


def plan_fuel_stops(
    candidates,
    route_distance_miles,
    mpg=10.0,
    max_range_miles=500,
    start_search_miles=15.0,
    stop_penalty_usd=0.0,
):
    """Choose stops and purchase amounts that minimise fuel cost plus stop penalties.

    The vehicle starts empty and first fills at the cheapest station within
    ``start_search_miles`` of the start (treated as being at the start). It may buy
    any amount at a stop, and the cheapest plan arrives with an empty tank, so the
    gallons bought equal ``route_distance_miles / mpg``.
    """
    stations = _cheapest_per_position(
        [candidate for candidate in candidates if candidate.route_mile < route_distance_miles]
    )
    start_options = [candidate for candidate in stations if candidate.route_mile <= start_search_miles]
    if not start_options:
        raise NoFuelPlan(f"No fuel station was found within {start_search_miles:g} route miles of the start.")
    first = min(start_options, key=lambda candidate: (candidate.price, candidate.route_mile))
    nodes = [first, *(candidate for candidate in stations if candidate.route_mile > first.route_mile)]

    units = FUEL_UNITS_PER_MILE
    capacity = int(round(max_range_miles * units))
    positions = [0, *(int(round(node.route_mile * units)) for node in nodes[1:])]
    destination = max(int(round(route_distance_miles * units)), positions[-1])
    positions.append(destination)
    levels = np.arange(capacity + 1)

    arrival = np.full(capacity + 1, np.inf)
    arrival[0] = 0.0  # Empty tank at the start.
    bought_from = []  # Per node: arrival level before buying, for each departure level (-1 = passed).
    for index, node in enumerate(nodes):
        price_per_unit = node.price / mpg / units
        # Cheapest way to leave with ``f`` units after buying here:
        #   min over g <= f of arrival[g] + price * (f - g) + penalty.
        adjusted = arrival - price_per_unit * levels
        running_min = np.minimum.accumulate(adjusted)
        source_level = np.maximum.accumulate(np.where(adjusted == running_min, levels, 0))
        with_stop = running_min + price_per_unit * levels + stop_penalty_usd
        stop_here = with_stop < arrival - 1e-9  # Ignore float noise on zero-gallon "stops".
        departure = np.where(stop_here, with_stop, arrival)
        bought_from.append(np.where(stop_here, source_level, -1))

        leg = positions[index + 1] - positions[index]
        arrival = np.full(capacity + 1, np.inf)
        if leg <= capacity:
            arrival[: capacity + 1 - leg] = departure[leg:]
        if not np.isfinite(arrival).any():
            raise NoFuelPlan(
                f"No fuel station in the data is within {max_range_miles} miles after route mile "
                f"{node.route_mile:.0f} (next stop is {leg / units:.0f} miles away)."
            )

    # Walk back from the destination, reconstructing where fuel was bought.
    level = int(np.argmin(arrival))
    purchases = []
    for index in range(len(nodes) - 1, -1, -1):
        departure_level = level + positions[index + 1] - positions[index]
        arrival_level = int(bought_from[index][departure_level])
        if arrival_level < 0 or arrival_level == departure_level:
            level = departure_level
            continue
        gallons = (departure_level - arrival_level) / units / mpg
        purchases.append(
            {
                "candidate": nodes[index],
                "fuel_on_arrival_gallons": arrival_level / units / mpg,
                "gallons": gallons,
                "cost": gallons * nodes[index].price,
            }
        )
        level = arrival_level
    purchases.reverse()

    return {
        "purchases": purchases,
        "total_gallons": sum(purchase["gallons"] for purchase in purchases),
        "total_cost": sum(purchase["cost"] for purchase in purchases),
        "candidate_station_count": len(candidates),
    }
