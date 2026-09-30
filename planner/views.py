from django.db import connection
from rest_framework.response import Response
from rest_framework.views import APIView

from .pipeline import plan_trip
from .serializers import PlanRequestSerializer, PlanResponseSerializer


class PlanRouteView(APIView):
    def post(self, request):
        req = PlanRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = plan_trip(req.validated_data["start"], req.validated_data["finish"])
        return Response(PlanResponseSerializer(data).data)


class HealthView(APIView):
    throttle_classes: list = []

    def get(self, request):
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        return Response({"status": "ok"})
