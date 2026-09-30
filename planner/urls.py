from django.urls import path

from . import views

urlpatterns = [
    path("routes/plan/", views.PlanRouteView.as_view(), name="plan-route"),
    path("health/", views.HealthView.as_view(), name="health"),
]
