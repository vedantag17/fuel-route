import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routes.services.places import build_place_rows


class Command(BaseCommand):
    help = (
        "Build data/us_places.csv from Census Gazetteer place and county-subdivision files "
        "(https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html)."
    )

    def add_arguments(self, parser):
        parser.add_argument("places_file", help="Path to YYYY_Gaz_place_national.txt")
        parser.add_argument(
            "county_subdivisions_file",
            nargs="?",
            help="Optional path to YYYY_Gaz_cousubs_national.txt (adds New England towns).",
        )
        parser.add_argument("--output", default=str(settings.US_PLACES_CSV_PATH))

    def handle(self, *args, **options):
        for key in ("places_file", "county_subdivisions_file"):
            if options[key] and not Path(options[key]).exists():
                raise CommandError(f"File not found: {options[key]}")

        rows = build_place_rows(options["places_file"], options["county_subdivisions_file"])
        output = Path(options["output"])
        with output.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(["state", "name", "latitude", "longitude"])
            writer.writerows(rows)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} US places to {output}."))
