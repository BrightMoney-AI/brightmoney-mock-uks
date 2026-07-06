"""Serve the ops dashboard SPA at /dashboard (design §8 companion).

A single self-contained HTML page that drives the existing /mock/admin/* JSON
API from the browser — no new server-side logic, so anything the dashboard does
is exactly what the admin API already exposes (bundles, scenarios CRUD, call
log, per-host/run_id parallel isolation).

Gated behind the same ``MOCKVENDOR_ADMIN_ENABLED`` flag as the admin API: if the
API is off the dashboard would be inert anyway. The HTML is read from disk per
request so it can be edited without restarting the server.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden

_HTML_PATH = Path(__file__).with_name("dashboard.html")


def dashboard(request):
    if not bool(getattr(settings, "MOCKVENDOR_ADMIN_ENABLED", False)):
        return HttpResponseForbidden("mock admin dashboard disabled "
                                     "(set MOCKVENDOR_ADMIN_ENABLED=1)")
    return HttpResponse(_HTML_PATH.read_text(encoding="utf-8"),
                        content_type="text/html; charset=utf-8")
