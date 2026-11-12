"""The admin pages, under ``OPENCS_WEB_ADMIN_PATH`` (``staffviews``)."""

from django.urls import path

from opencs_web import staffviews

app_name = "staff"

urlpatterns = [
    path("", staffviews.home, name="home"),
    path("login", staffviews.login, name="login"),
    path("logout", staffviews.logout, name="logout"),
    path("audit", staffviews.audit_log, name="audit"),
    path("do/<str:action>", staffviews.act, name="act"),
]
