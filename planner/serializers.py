"""Request validation and the response contract. Rounding for display happens here."""

import math
from decimal import ROUND_HALF_UP

from rest_framework import serializers


def dec(places: int) -> serializers.DecimalField:
    # coerce_to_string (DRF default) keeps client-side floats out of money/gallons
    return serializers.DecimalField(max_digits=14, decimal_places=places, rounding=ROUND_HALF_UP)


class LocationField(serializers.Field):
    """ "City, ST" string or {"lat": .., "lng": ..} object."""

    default_error_messages = {
        "invalid": 'Provide "City, ST" or an object like {{"lat": 41.88, "lng": -87.63}}.',
        "range": "lat must be within [-90, 90] and lng within [-180, 180].",
    }

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip():
            return data.strip()
        if isinstance(data, dict) and set(data) == {"lat", "lng"}:
            try:
                if isinstance(data["lat"], bool) or isinstance(data["lng"], bool):
                    raise TypeError
                lat, lng = float(data["lat"]), float(data["lng"])
            except (TypeError, ValueError):
                self.fail("invalid")
            if not (
                math.isfinite(lat)
                and math.isfinite(lng)
                and -90 <= lat <= 90
                and -180 <= lng <= 180
            ):
                self.fail("range")
            return {"lat": lat, "lng": lng}
        self.fail("invalid")

    def to_representation(self, value):
        return value


class PlanRequestSerializer(serializers.Serializer):
    start = LocationField()
    finish = LocationField()


class PointSerializer(serializers.Serializer):
    label = serializers.CharField()
    lat = serializers.FloatField()
    lng = serializers.FloatField()


class RouteSerializer(serializers.Serializer):
    start = PointSerializer()
    finish = PointSerializer()
    distance_miles = serializers.FloatField()
    duration_minutes = serializers.FloatField()
    geometry = serializers.JSONField()  # GeoJSON LineString, coordinates are [lng, lat]


class StationSerializer(serializers.Serializer):
    opis_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    lat = serializers.FloatField()
    lng = serializers.FloatField()


class StopSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    station = StationSerializer()
    mile_marker = serializers.FloatField()
    distance_from_route_miles = serializers.FloatField()
    price_per_gallon = dec(4)
    gallons_on_arrival = dec(3)
    gallons_purchased = dec(3)
    gallons_after_purchase = dec(3)
    cost = dec(2)


class VehicleSerializer(serializers.Serializer):
    range_miles = serializers.FloatField()
    mpg = serializers.FloatField()
    tank_gallons = dec(3)


class FuelSerializer(serializers.Serializer):
    vehicle = VehicleSerializer()
    start_gallons = dec(3)
    gallons_consumed = dec(3)
    gallons_purchased = dec(3)
    gallons_remaining_at_destination = dec(3)
    total_fuel_cost = dec(2)
    stops = StopSerializer(many=True)


class MetaSerializer(serializers.Serializer):
    algorithm = serializers.CharField()
    candidate_stations = serializers.IntegerField()
    route_cached = serializers.BooleanField()
    corridor_miles = serializers.FloatField()
    assumptions = serializers.ListField(child=serializers.CharField())


class PlanResponseSerializer(serializers.Serializer):
    route = RouteSerializer()
    fuel = FuelSerializer()
    meta = MetaSerializer()
