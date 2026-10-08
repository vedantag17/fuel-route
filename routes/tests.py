from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from .models import FuelStation
from .services.optimizer import Candidate, NoFuelPlan, find_corridor_candidates, plan_fuel_stops
from .services.places import lookup_place, normalize_place_name, parse_city_state
from .services.stations import StationIndex


def candidate(mile, price, index=0, off_route=0.0):
    return Candidate(index=index, route_mile=mile, off_route_miles=off_route, price=price)


class PlanFuelStopsTests(SimpleTestCase):
    def test_carries_cheap_fuel_past_an_expensive_station(self):
        # A ($2) -> B ($5, mile 300) -> C ($4, mile 600) -> finish at mile 800, 500-mile range.
        # Best: fill 50 gal at A, buy 10 gal at B to reach C, buy 20 gal at C = $230.
        stations = [candidate(0, 2.0, 0), candidate(300, 5.0, 1), candidate(600, 4.0, 2)]
        plan = plan_fuel_stops(stations, 800, mpg=10, max_range_miles=500)
        self.assertAlmostEqual(plan["total_cost"], 230.0, places=6)
        self.assertEqual([round(p["gallons"], 6) for p in plan["purchases"]], [50.0, 10.0, 20.0])
        self.assertEqual(plan["purchases"][1]["fuel_on_arrival_gallons"], 20.0)

    def test_gallons_bought_equal_route_fuel_use(self):
        stations = [candidate(mile, 3.0 + (mile % 7) / 10, index) for index, mile in enumerate(range(0, 2000, 90))]
        plan = plan_fuel_stops(stations, 2000, mpg=10, max_range_miles=500)
        self.assertAlmostEqual(plan["total_gallons"], 200.0, places=6)
        self.assertAlmostEqual(
            plan["total_cost"], sum(p["gallons"] * p["candidate"].price for p in plan["purchases"]), places=6
        )

    def test_short_trip_is_one_stop(self):
        plan = plan_fuel_stops([candidate(2, 3.5), candidate(100, 3.0, 1)], 150, mpg=10, max_range_miles=500,
                               stop_penalty_usd=5)
        self.assertEqual(len(plan["purchases"]), 1)
        self.assertAlmostEqual(plan["total_gallons"], 15.0)

    def test_stop_penalty_skips_stops_that_save_cents(self):
        stations = [candidate(0, 3.00, 0), candidate(50, 2.99, 1), candidate(100, 2.98, 2)]
        cheapest = plan_fuel_stops(stations, 400, mpg=10, max_range_miles=500, stop_penalty_usd=0)
        practical = plan_fuel_stops(stations, 400, mpg=10, max_range_miles=500, stop_penalty_usd=5)
        self.assertEqual(len(cheapest["purchases"]), 3)
        self.assertEqual(len(practical["purchases"]), 1)
        self.assertLess(cheapest["total_cost"], practical["total_cost"])

    def test_gap_longer_than_range_has_no_plan(self):
        with self.assertRaisesMessage(NoFuelPlan, "within 500 miles"):
            plan_fuel_stops([candidate(0, 3.0), candidate(600, 3.0, 1)], 900, max_range_miles=500)

    def test_requires_a_station_near_the_start(self):
        with self.assertRaisesMessage(NoFuelPlan, "of the start"):
            plan_fuel_stops([candidate(40, 3.0)], 300, start_search_miles=15)


class CorridorTests(SimpleTestCase):
    def test_projects_stations_onto_route_and_filters_by_distance(self):
        # Straight route along latitude 40 from -100 to -90 longitude (~530 miles).
        route = [[-100 + step / 10, 40.0] for step in range(101)]
        index = StationIndex(
            ids=np.array([1, 2, 3]),
            longitudes=np.array([-95.0, -95.0, -99.0]),
            latitudes=np.array([40.05, 41.0, 40.0]),  # ~3.5 mi off, ~69 mi off, on the line
            prices=np.array([3.1, 2.0, 3.3]),
            version=(3, 3),
        )
        found = find_corridor_candidates(route, 530.0, index, corridor_miles=15)
        self.assertEqual([c.index for c in found], [2, 0])
        self.assertAlmostEqual(found[0].route_mile, 53.0, delta=1)
        self.assertAlmostEqual(found[1].route_mile, 265.0, delta=2)
        self.assertAlmostEqual(found[1].off_route_miles, 3.45, delta=0.2)


class PlaceLookupTests(SimpleTestCase):
    def test_normalizes_common_spelling_variants(self):
        self.assertEqual(normalize_place_name("St. Louis"), normalize_place_name("Saint Louis"))
        self.assertEqual(normalize_place_name("Mc Calla"), normalize_place_name("McCalla"))
        self.assertEqual(normalize_place_name("La Place"), normalize_place_name("LaPlace"))

    def test_parses_city_state_inputs(self):
        self.assertEqual(parse_city_state("Austin, TX"), ("Austin", "TX"))
        self.assertEqual(parse_city_state("Saint Louis, Missouri 63101"), ("Saint Louis", "MO"))
        self.assertEqual(parse_city_state("Chicago, IL, USA"), ("Chicago", "IL"))
        self.assertIsNone(parse_city_state("350 5th Ave, New York, NY 10118"))
        self.assertIsNone(parse_city_state("Toronto, ON"))

    def test_looks_up_bundled_census_places(self):
        latitude, longitude, name = lookup_place("New York", "NY")
        self.assertAlmostEqual(latitude, 40.66, delta=0.1)
        self.assertAlmostEqual(longitude, -73.94, delta=0.1)
        self.assertIsNotNone(lookup_place("Nashville", "Tennessee"))
        self.assertIsNone(lookup_place("Gotham", "FL"))


@mock.patch("routes.views.get_driving_route")
class RoutePlanApiTests(TestCase):
    URL = "/api/v1/route-plan/"

    @classmethod
    def setUpTestData(cls):
        for number, (longitude, price) in enumerate([(-99.95, 3.40), (-97.0, 2.90), (-93.0, 3.80), (-91.0, 3.20)]):
            FuelStation.objects.create(
                source_id=str(number), name=f"Station {number}", city="Town", state="KS",
                price_per_gallon=price, latitude=40.0, longitude=longitude,
                geocode_status="city", fingerprint=f"test-{number}",
            )

    def setUp(self):
        cache.clear()

    @staticmethod
    def fake_route(start, finish):
        coordinates = [[-100 + step / 10, 40.0] for step in range(101)]
        return {
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "distance_meters": 530 * 1609.344,
            "duration_seconds": 8 * 3600,
            "from_cache": False,
        }

    def test_plans_route_with_stops_and_total_cost(self, get_route):
        get_route.side_effect = self.fake_route
        response = self.client.post(
            self.URL, {"start_address": "40.0,-100.0", "finish_address": "40.0,-90.0"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["fuel_plan"]["status"], "available")
        self.assertEqual(data["summary"]["total_gallons"], 53.0)
        self.assertEqual([stop["name"] for stop in data["fuel_stops"]], ["Station 0", "Station 1"])
        self.assertAlmostEqual(
            data["summary"]["total_fuel_cost_usd"], sum(stop["cost_usd"] for stop in data["fuel_stops"]), delta=0.02
        )
        self.assertEqual(data["route"]["geometry"]["type"], "LineString")
        self.assertIn("?start=40.0%2C-100.0", data["map_url"])
        self.assertEqual(data["meta"]["external_api_calls"], 1)  # Coordinates need no geocoding.

    def test_repeat_request_is_served_from_cache(self, get_route):
        get_route.side_effect = self.fake_route
        params = {"start_address": "40.0,-100.0", "finish_address": "40.0,-90.0"}
        self.client.get(self.URL, params)
        response = self.client.get(self.URL, params)
        self.assertTrue(response.json()["meta"]["cached"])
        self.assertEqual(get_route.call_count, 1)

    def test_city_inputs_resolve_without_geocoder(self, get_route):
        get_route.side_effect = self.fake_route
        with mock.patch("routes.services.geocoding.requests.get") as census:
            response = self.client.post(
                self.URL, {"start_address": "Denver, CO", "finish_address": "Kansas City, MO"},
                content_type="application/json",
            )
        census.assert_not_called()
        self.assertEqual(response.json()["summary"]["start"], "Denver, CO")

    def test_validation_and_unknown_city_errors(self, get_route):
        response = self.client.post(self.URL, {"start_address": "Denver, CO"}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            self.URL, {"start_address": "Gotham, FL", "finish_address": "Denver, CO"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 422)
        get_route.assert_not_called()

    def test_health_reports_coverage(self, get_route):
        data = self.client.get("/api/v1/health/").json()
        self.assertEqual(data["fuel_stations"]["located_stations"], 4)
