from django.urls import path

from opencs_web import views

urlpatterns = [
    path("healthz", views.health, name="health"),
]
