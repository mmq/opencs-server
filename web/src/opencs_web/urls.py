from django.urls import path

from opencs_web import views

urlpatterns = [
    path("", views.home, name="home"),
    path("status/events", views.status_events, name="status-events"),
    path("assets/<str:name>", views.asset, name="asset"),
    path("healthz", views.health, name="health"),
]
