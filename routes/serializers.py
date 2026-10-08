from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    start_address = serializers.CharField(max_length=400, trim_whitespace=True)
    finish_address = serializers.CharField(max_length=400, trim_whitespace=True)
    mpg = serializers.FloatField(default=10.0, min_value=1.0, max_value=100.0)
    max_range_miles = serializers.IntegerField(default=500, min_value=1, max_value=500)
    corridor_miles = serializers.FloatField(default=15.0, min_value=1.0, max_value=100.0)
    stop_penalty_usd = serializers.FloatField(
        default=5.0,
        min_value=0.0,
        max_value=500.0,
        help_text="Cost-equivalent of making one extra stop; 0 gives the pure minimum fuel cost.",
    )
