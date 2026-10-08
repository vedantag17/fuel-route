import csv
import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routes.constants import US_STATE_CODES
from routes.models import FuelStation
from routes.services.places import lookup_place


def first_value(row, *names):
    normalized = {key.strip().casefold(): value for key, value in row.items() if key}
    for name in names:
        value = normalized.get(name.casefold())
        if value not in (None, ""):
            return value.strip()
    return ""


class Command(BaseCommand):
    help = (
        "Replace stored fuel stations with the assessment CSV. Rows are deduplicated by "
        "OPIS Truckstop ID (keeping the lowest price) and located at their city's Census "
        "internal point unless the CSV supplies Latitude/Longitude columns."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?")

    def handle(self, *args, **options):
        path = Path(options["csv_path"]) if options["csv_path"] else settings.FUEL_CSV_PATH
        if not path.exists():
            raise CommandError(f"CSV file not found: {path}")

        cheapest_by_id = {}
        row_count = invalid = non_us = 0
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            required = {"opis truckstop id", "truckstop name", "retail price"}
            headers = {header.strip().casefold() for header in (reader.fieldnames or [])}
            if not required.issubset(headers):
                raise CommandError("CSV must include OPIS Truckstop ID, Truckstop Name, and Retail Price columns.")

            for line_number, row in enumerate(reader, start=2):
                row_count += 1
                state = first_value(row, "State").upper()
                if state not in US_STATE_CODES:
                    non_us += 1
                    continue
                try:
                    price = Decimal(first_value(row, "Retail Price"))
                    latitude_text = first_value(row, "Latitude", "Lat")
                    longitude_text = first_value(row, "Longitude", "Lon", "Lng")
                    latitude = float(latitude_text) if latitude_text else None
                    longitude = float(longitude_text) if longitude_text else None
                    if price <= 0 or (latitude is None) != (longitude is None):
                        raise ValueError("invalid price or incomplete coordinates")
                    if latitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                        raise ValueError("coordinates are out of range")
                except (InvalidOperation, TypeError, ValueError):
                    invalid += 1
                    self.stderr.write(f"Skipped invalid CSV row {line_number}.")
                    continue

                # The same OPIS ID appears with several names/prices; keep its cheapest row.
                source_id = first_value(row, "OPIS Truckstop ID") or f"row-{line_number}"
                current = cheapest_by_id.get(source_id)
                if current is not None and current["price"] <= price:
                    continue
                cheapest_by_id[source_id] = {
                    "row": row,
                    "state": state,
                    "price": price,
                    "latitude": latitude,
                    "longitude": longitude,
                }

        stations = []
        located_by_city = 0
        for source_id, item in cheapest_by_id.items():
            row = item["row"]
            city = first_value(row, "City")
            latitude, longitude = item["latitude"], item["longitude"]
            if latitude is not None:
                status, match_type, located_name = "provided", "Provided", ""
            elif place := lookup_place(city, item["state"]):
                latitude, longitude, matched_name = place
                status, match_type = "city", "City_Centroid"
                located_name = f"{matched_name}, {item['state']}"
                located_by_city += 1
            else:
                status, match_type, located_name = "pending", "", ""
            stations.append(
                FuelStation(
                    source_id=source_id,
                    name=first_value(row, "Truckstop Name") or "Unnamed truck stop",
                    address=first_value(row, "Address"),
                    city=city,
                    state=item["state"],
                    rack_id=first_value(row, "Rack ID"),
                    price_per_gallon=item["price"],
                    latitude=latitude,
                    longitude=longitude,
                    geocode_match_type=match_type,
                    geocoded_address=located_name,
                    geocode_status=status,
                    fingerprint=hashlib.sha256(f"opis:{source_id}".encode()).hexdigest(),
                )
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)

        pending = sum(1 for station in stations if station.geocode_status == "pending")
        self.stdout.write(
            self.style.SUCCESS(
                f"Read {row_count} rows; stored {len(stations)} unique US stations "
                f"({row_count - non_us - invalid - len(stations)} repeated station-ID rows merged, "
                f"{non_us} non-US and {invalid} invalid rows skipped). "
                f"Located {len(stations) - pending} stations ({located_by_city} by city centroid); "
                f"{pending} still need coordinates (optional: geocode_fuel_stations)."
            )
        )
