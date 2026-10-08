"""Offline US place lookup built from the public-domain Census Gazetteer files.

The fuel CSV only gives highway/exit descriptions plus a city and state, so each
station is located at its city's Census internal point. The same index resolves
"City, ST" route inputs without calling an external geocoder.
"""

import csv
import re
from functools import lru_cache

from django.conf import settings

from ..constants import US_STATE_CODES, US_STATE_NAMES

_TOKEN_ALIASES = {
    "saint": "st",
    "sainte": "ste",
    "fort": "ft",
    "mount": "mt",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}
_GAZETTEER_SUFFIX = re.compile(
    r"\s+(?:city and borough|city|town|village|borough|CDP|municipality|township|"
    r"plantation|CCD|UT|gore|grant|location|purchase|comunidad|zona urbana|"
    r"(?:unified|consolidated|metro|metropolitan) government)"
    r"(?:\s+\(balance\))?$|\s+\(balance\)$",
    re.IGNORECASE,
)
# Lower rank wins when two Gazetteer rows normalize to the same state/name key.
_RANK_INCORPORATED, _RANK_CDP, _RANK_ALIAS, _RANK_COUNTY_SUBDIVISION = 0, 1, 2, 3


def normalize_place_name(name):
    text = name.casefold().replace(".", " ").replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    # Spaces are dropped so "La Place"/"LaPlace" and "Mc Calla"/"McCalla" agree.
    return "".join(_TOKEN_ALIASES.get(token, token) for token in text.split())


def _gazetteer_rows(path):
    with open(path, encoding="utf-8", newline="") as source:
        reader = csv.reader(source, delimiter="|")
        headers = [header.strip() for header in next(reader)]
        for values in reader:
            yield dict(zip(headers, (value.strip() for value in values)))


def build_place_rows(places_path, county_subdivisions_path=None):
    """Return (state, name, latitude, longitude) rows, one per normalized key."""
    best = {}

    def offer(state, display_name, latitude, longitude, rank, land_area):
        key = (state, normalize_place_name(display_name))
        if not key[1]:
            return
        score = (rank, -land_area)
        if key not in best or score < best[key][0]:
            best[key] = (score, (state, display_name, latitude, longitude))

    sources = [(places_path, False)]
    if county_subdivisions_path:
        sources.append((county_subdivisions_path, True))
    for path, is_county_subdivision in sources:
        for row in _gazetteer_rows(path):
            state = row["USPS"]
            if state not in US_STATE_CODES:
                continue
            full_name = row["NAME"]
            name = _GAZETTEER_SUFFIX.sub("", full_name).strip()
            latitude = round(float(row["INTPTLAT"]), 6)
            longitude = round(float(row["INTPTLONG"]), 6)
            land_area = float(row.get("ALAND") or 0)
            if is_county_subdivision:
                rank = _RANK_COUNTY_SUBDIVISION
            elif full_name.endswith(" CDP"):
                rank = _RANK_CDP
            else:
                rank = _RANK_INCORPORATED
            offer(state, name, latitude, longitude, rank, land_area)
            # "Nashville-Davidson metropolitan government (balance)" is the
            # Gazetteer name for Nashville; also index each hyphenated part.
            if "(balance)" in full_name:
                for part in re.split(r"[-/]", name):
                    if part.strip() and part.strip() != name:
                        offer(state, part.strip(), latitude, longitude, _RANK_ALIAS, land_area)
    return sorted(row for _, row in best.values())


_INPUT_SUFFIX = re.compile(r"\s+(?:township|twp|city|town|village|national park)\.?$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _place_index():
    index = {}
    with open(settings.US_PLACES_CSV_PATH, encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            index[(row["state"], normalize_place_name(row["name"]))] = (
                float(row["latitude"]),
                float(row["longitude"]),
                row["name"],
            )
    return index


def lookup_place(city, state):
    """Return (latitude, longitude, matched_name) for a US city, or None."""
    state_code = normalize_state(state)
    if not state_code or not city:
        return None
    index = _place_index()
    place = index.get((state_code, normalize_place_name(city)))
    if place is None:
        trimmed = _INPUT_SUFFIX.sub("", city).strip()
        if trimmed and trimmed != city.strip():
            place = index.get((state_code, normalize_place_name(trimmed)))
    return place


def normalize_state(state):
    text = " ".join(state.replace(".", "").split()).upper()
    if text in US_STATE_CODES:
        return text
    return US_STATE_NAMES.get(text)


_CITY_STATE_INPUT = re.compile(
    r"^\s*(?P<city>[^,\d][^,]*?)\s*,\s*(?P<state>[A-Za-z][A-Za-z .]*?)"
    r"(?:\s+\d{5}(?:-\d{4})?)?(?:\s*,\s*(?:USA|US|United States(?: of America)?))?\s*$",
    re.IGNORECASE,
)


def parse_city_state(text):
    """Split inputs such as "Austin, TX" or "Saint Louis, Missouri 63101"."""
    match = _CITY_STATE_INPUT.match(text)
    if not match:
        return None
    state_code = normalize_state(match.group("state"))
    if not state_code:
        return None
    return match.group("city").strip(), state_code
