"""In-process cache of located US fuel stations as NumPy arrays.

Route requests only need coordinates and prices for the corridor search, so the
table is read once per process and reloaded only when the imported data changes.
"""

import threading
from dataclasses import dataclass

import numpy as np
from django.db.models import Count, Max

from ..constants import US_STATE_CODES
from ..models import FuelStation


@dataclass(frozen=True)
class StationIndex:
    ids: np.ndarray
    longitudes: np.ndarray
    latitudes: np.ndarray
    prices: np.ndarray
    version: tuple

    def __len__(self):
        return len(self.ids)


_lock = threading.Lock()
_cached = None


def located_stations():
    return FuelStation.objects.filter(
        state__in=US_STATE_CODES, latitude__isnull=False, longitude__isnull=False
    )


def get_station_index():
    global _cached
    queryset = located_stations()
    summary = queryset.aggregate(count=Count("pk"), max_pk=Max("pk"))
    version = (summary["count"], summary["max_pk"])
    if _cached is not None and _cached.version == version:
        return _cached
    with _lock:
        if _cached is None or _cached.version != version:
            rows = list(queryset.order_by("pk").values_list("pk", "longitude", "latitude", "price_per_gallon"))
            columns = list(zip(*rows)) if rows else [(), (), (), ()]
            _cached = StationIndex(
                ids=np.array(columns[0], dtype=np.int64),
                longitudes=np.array(columns[1], dtype=np.float64),
                latitudes=np.array(columns[2], dtype=np.float64),
                prices=np.array([float(price) for price in columns[3]], dtype=np.float64),
                version=version,
            )
        return _cached
