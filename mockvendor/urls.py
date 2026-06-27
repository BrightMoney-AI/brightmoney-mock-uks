"""mockvendor admin/seed API URLConf — mounted under /mock/admin/ (design §8)."""
from __future__ import annotations

from django.urls import path

from . import admin_api

urlpatterns = [
    path("scenarios", admin_api.scenarios),
    path("scenarios/<int:scenario_id>", admin_api.scenario_detail),
    path("reset", admin_api.reset),
    path("reset/scenarios", admin_api.reset_scenarios),
    path("calls", admin_api.calls),
    path("formats", admin_api.formats),
]
