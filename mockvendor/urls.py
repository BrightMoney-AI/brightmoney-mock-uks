"""mockvendor admin/seed API URLConf — mounted under /mock/admin/ (design §8)."""
from __future__ import annotations

from django.urls import path

from . import admin_api, tests_api

urlpatterns = [
    path("scenarios", admin_api.scenarios),
    path("scenarios/<int:scenario_id>", admin_api.scenario_detail),
    path("reset", admin_api.reset),
    path("reset/scenarios", admin_api.reset_scenarios),
    path("register", admin_api.register),
    path("register/<str:bundle_id>", admin_api.register_detail),
    path("implement", admin_api.implement),
    path("calls", admin_api.calls),
    path("formats", admin_api.formats),
    # dashboard test runner
    path("test-csvs", tests_api.test_csvs),
    path("testruns", tests_api.testruns),
    path("testruns/<int:run_id>", tests_api.testrun_detail),
]
