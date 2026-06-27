"""Django admin registration for every model (design §8)."""
from __future__ import annotations

from django.contrib import admin

from .models import CallLog, Endpoint, Format, Response, Scenario


class ResponseInline(admin.TabularInline):
    model = Response
    extra = 1


@admin.register(Format)
class FormatAdmin(admin.ModelAdmin):
    list_display = ("name", "content_type", "serializer_path")


@admin.register(Endpoint)
class EndpointAdmin(admin.ModelAdmin):
    list_display = ("method", "path_pattern", "default_format", "enabled")
    list_filter = ("method", "enabled")
    search_fields = ("path_pattern",)


@admin.register(Scenario)
class ScenarioAdmin(admin.ModelAdmin):
    list_display = ("name", "endpoint", "priority", "is_sequence", "enabled", "match_key", "match_value", "run_id")
    list_filter = ("is_sequence", "enabled")
    search_fields = ("name", "match_value")
    inlines = [ResponseInline]


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "request_method", "request_path", "response_status", "scenario", "delay_applied_ms")
    list_filter = ("request_method", "response_status")
    search_fields = ("request_path",)
