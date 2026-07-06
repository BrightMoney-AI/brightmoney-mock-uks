"""Root URLConf.

  /admin/                  Django admin (manage scenarios via ModelAdmin)
  /mock/admin/...          mockvendor DRF admin & seed API (design §8)
  /dashboard               mockvendor ops dashboard SPA (bundles/scenarios/calls)
  /aut/...                 the dummy Application Under Test (demo only)
  /<anything-else>         mockvendor catch-all serve view (design §3.1)

The catch-all is mounted LAST so the admin/AUT/dashboard routes win first.
"""
from __future__ import annotations

from django.contrib import admin
from django.urls import include, path, re_path

from mockvendor import dashboard_views
from mockvendor import views as mock_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("mock/admin/", include("mockvendor.urls")),
    path("dashboard", dashboard_views.dashboard, name="mock-dashboard"),
    path("dashboard/", dashboard_views.dashboard),
    path("aut/", include("dummy_aut.urls")),
    # Catch-all: any other path is treated as a vendor endpoint to mock.
    re_path(r"^.*$", mock_views.serve, name="mock-serve"),
]
