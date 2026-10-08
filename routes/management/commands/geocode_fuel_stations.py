import csv
import io

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routes.models import FuelStation


class Command(BaseCommand):
    help = "Batch-geocode imported fuel stations once with the US Census Geocoder."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Only process this many unlocated stations (0 processes all).",
        )
        parser.add_argument(
            "--retry-no-matches",
            action="store_true",
            help="Retry rows previously marked no_match after changing their address data.",
        )

    def handle(self, *args, **options):
        if options["retry_no_matches"]:
            FuelStation.objects.filter(geocode_status="no_match").update(geocode_status="pending")
        pending = FuelStation.objects.filter(geocode_status="pending").order_by("pk")
        if options["limit"]:
            pending = pending[: options["limit"]]
        stations = list(pending)
        if not stations:
            unlocated = FuelStation.objects.filter(latitude__isnull=True).count()
            located = FuelStation.objects.filter(latitude__isnull=False).count()
            self.stdout.write(
                f"No pending addresses. {located} rows have coordinates; {unlocated} remain unmatched. "
                "Use --retry-no-matches after improving their address data."
            )
            return

        matched_count = 0
        no_match_count = 0
        # Census batch requests support up to 10,000 input records. The assessment
        # file currently fits in one request, avoiding per-station geocoder calls.
        for offset in range(0, len(stations), 10_000):
            batch = stations[offset : offset + 10_000]
            input_buffer = io.StringIO()
            writer = csv.writer(input_buffer, lineterminator="\n")
            for station in batch:
                writer.writerow(
                    [station.pk, station.address, station.city, station.state, ""]
                )

            try:
                response = requests.post(
                    settings.CENSUS_GEOCODER_BATCH_URL,
                    data={"benchmark": settings.CENSUS_GEOCODER_BENCHMARK},
                    files={
                        "addressFile": (
                            "fuel-stations.csv",
                            input_buffer.getvalue().encode("utf-8"),
                            "text/csv",
                        )
                    },
                    timeout=(10, 120),
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise CommandError(f"Census batch geocoder request failed: {exc}") from exc

            reader = csv.reader(io.StringIO(response.text))
            returned_ids = set()
            for row in reader:
                if not row:
                    continue
                try:
                    station_id = int(row[0])
                except ValueError:
                    continue
                returned_ids.add(station_id)
                try:
                    station = FuelStation.objects.get(pk=station_id)
                except FuelStation.DoesNotExist:
                    continue
                if len(row) < 6 or row[2].strip().casefold() != "match":
                    no_match_count += 1
                    station.geocode_status = "no_match"
                    station.geocode_match_type = row[3].strip()[:32] if len(row) > 3 else "No_Match"
                    station.save(update_fields=["geocode_status", "geocode_match_type"])
                    continue
                try:
                    longitude_text, latitude_text = row[5].split(",", maxsplit=1)
                    longitude = float(longitude_text)
                    latitude = float(latitude_text)
                except (ValueError, TypeError):
                    no_match_count += 1
                    station.geocode_status = "no_match"
                    station.geocode_match_type = "Invalid_Result"
                    station.save(update_fields=["geocode_status", "geocode_match_type"])
                    continue

                station.longitude = longitude
                station.latitude = latitude
                station.geocode_match_type = row[3].strip()
                station.geocoded_address = row[4].strip()[:500]
                station.geocode_status = "matched"
                station.save(
                    update_fields=[
                        "longitude",
                        "latitude",
                        "geocode_match_type",
                        "geocoded_address",
                        "geocode_status",
                    ]
                )
                matched_count += 1
            missing_from_response = {station.pk for station in batch} - returned_ids
            if missing_from_response:
                FuelStation.objects.filter(pk__in=missing_from_response).update(
                    geocode_status="no_match", geocode_match_type="No_Response"
                )
                no_match_count += len(missing_from_response)

        self.stdout.write(
            self.style.SUCCESS(
                f"Geocoded {matched_count} stations; {no_match_count} rows had no usable match. "
                "Review the Census match type and locations before relying on route costs."
            )
        )
