from django.db import models
from django.db.models import Q


class Place(models.Model):
    """Reference geodata (Census Gazetteer) for "City, ST" lookups and station geocoding."""

    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120)
    state = models.CharField(max_length=2)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["state", "name_key"], name="place_state_key_uniq"),
            models.CheckConstraint(
                condition=Q(latitude__gte=24, latitude__lte=50)
                & Q(longitude__gte=-125, longitude__lte=-66),
                name="place_in_conus",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.state}"


class FuelStation(models.Model):
    """One row per OPIS truckstop id (the price file is a complete snapshot)."""

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=6, decimal_places=4)  # dollars per gallon
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Local record update timestamp: when the importer last changed this row (NOT the price date).
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(price__gt=0, price__lt=20), name="station_price_sane"
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name="station_coords_both_or_neither",
            ),
        ]
        indexes = [
            models.Index(
                fields=["latitude", "longitude"],
                name="station_lat_lng_idx",
                condition=Q(latitude__isnull=False),
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"
