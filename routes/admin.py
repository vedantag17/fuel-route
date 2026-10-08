from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("source_id", "name", "city", "state", "price_per_gallon", "latitude", "longitude")
    list_filter = ("state",)
    search_fields = ("source_id", "name", "city", "address")
