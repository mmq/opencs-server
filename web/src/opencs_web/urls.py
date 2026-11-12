from django.conf import settings
from django.urls import include, path

from opencs_web import accountviews, views

urlpatterns = [
    path("", views.home, name="home"),
    path("status/events", views.status_events, name="status-events"),
    path("register", accountviews.register, name="register"),
    path("register/<str:token>", accountviews.confirm, name="confirm"),
    path("login", accountviews.login, name="login"),
    path("login/<str:token>", accountviews.login_link, name="login-link"),
    path("logout", accountviews.logout, name="logout"),
    path("account", accountviews.account, name="account"),
    path("account/callsign", accountviews.change_callsign, name="change-callsign"),
    path("account/password", accountviews.new_password, name="new-password"),
    path("account/delete", accountviews.delete, name="delete"),
    path("privacy", accountviews.privacy, name="privacy"),
    path("assets/<str:name>", views.asset, name="asset"),
    path("healthz", views.health, name="health"),
    # The admin pages: a 404 each while OPENCS_WEB_ADMIN is off.
    path(f"{settings.OPENCS_ADMIN_PATH}/", include("opencs_web.staffurls")),
]
