from django.db import models


class FuelStation(models.Model):
    source_id = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=500, blank=True)
    city = models.CharField(max_length=150, blank=True)
    state = models.CharField(max_length=32, blank=True)
    rack_id = models.CharField(max_length=64, blank=True)
    price_per_gallon = models.DecimalField(max_digits=12, decimal_places=8)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_match_type = models.CharField(max_length=32, blank=True)
    geocoded_address = models.CharField(max_length=500, blank=True)
    geocode_status = models.CharField(max_length=16, default="pending")
    fingerprint = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["state", "city", "name"]
        indexes = [models.Index(fields=["state", "city"])]

    def __str__(self):
        return f"{self.name} — {self.city}, {self.state} (${self.price_per_gallon}/gal)"
