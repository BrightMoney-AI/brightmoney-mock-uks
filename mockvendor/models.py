"""Mock server data model (design doc §4).

Five models: Format, Endpoint, Scenario, Response, CallLog.
Stateful sequences are modelled as multiple Response rows under one Scenario,
ordered by ``seq_index`` — no separate sequence table.
"""
from __future__ import annotations

from django.db import models


class Format(models.Model):
    """Registry of supported body formats (design §4.3)."""

    name = models.CharField(max_length=32, unique=True)  # 'json', 'xml'
    content_type = models.CharField(max_length=128)
    serializer_path = models.CharField(max_length=255)  # dotted import path

    class Meta:
        db_table = "mockvendor_format"

    def __str__(self) -> str:
        return self.name


class Endpoint(models.Model):
    """A method + path pattern the mock can serve."""

    method = models.CharField(max_length=10, default="POST")
    path_pattern = models.CharField(max_length=255)  # e.g. /vendor/idology/verify
    default_format = models.ForeignKey(Format, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "mockvendor_endpoint"
        unique_together = ("method", "path_pattern")
        indexes = [models.Index(fields=["method", "path_pattern"])]

    def __str__(self) -> str:
        return f"{self.method} {self.path_pattern}"


class Scenario(models.Model):
    """A named, prioritised reply set bound to an endpoint (design §4.3)."""

    endpoint = models.ForeignKey(
        Endpoint, related_name="scenarios", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=120)
    priority = models.IntegerField(default=0)
    is_sequence = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    # Optional discriminator — a single body/header field, key is a JSONPath.
    match_key = models.CharField(max_length=120, blank=True)
    match_value = models.CharField(max_length=255, blank=True)
    # Runtime cursor for stateful sequences (which response to serve next).
    seq_cursor = models.IntegerField(default=0)
    # Optional namespacing for shared-mock / parallel CI isolation (design §8.1).
    run_id = models.CharField(max_length=120, blank=True, db_index=True)

    class Meta:
        db_table = "mockvendor_scenario"
        indexes = [models.Index(fields=["endpoint", "priority"])]

    def __str__(self) -> str:
        return self.name


class Response(models.Model):
    """The reply a scenario returns (design §4.3)."""

    scenario = models.ForeignKey(
        Scenario, related_name="responses", on_delete=models.CASCADE
    )
    seq_index = models.IntegerField(null=True, blank=True)  # null = single
    status_code = models.IntegerField(default=200)
    format = models.ForeignKey(Format, on_delete=models.PROTECT)
    canonical = models.JSONField(null=True, blank=True)  # format-neutral body
    raw_override = models.TextField(blank=True)  # byte-exact body
    headers = models.JSONField(default=dict, blank=True)
    delay_ms = models.IntegerField(default=0)

    class Meta:
        db_table = "mockvendor_response"
        ordering = ["seq_index", "id"]

    def __str__(self) -> str:
        return f"resp<{self.scenario.name}#{self.seq_index}>={self.status_code}"


class ScenarioBundle(models.Model):
    """A named, reusable group of scenario definitions (design §8: quick re-seed presets).

    ``POST /mock/admin/register`` saves the definition under ``bundle_id`` and
    seeds it immediately; ``POST /mock/admin/implement`` clears active scenarios
    (never CallLog — same guarantee as ``reset_scenarios``) and replays it.
    """

    bundle_id = models.CharField(max_length=120, unique=True)
    definition = models.JSONField()  # list of scenario payload dicts (seed.seed_scenario_dict shape)
    run_id = models.CharField(max_length=120, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mockvendor_scenariobundle"

    def __str__(self) -> str:
        return self.bundle_id


class CallLog(models.Model):
    """Append-only record of every received request (design §4.3)."""

    endpoint = models.ForeignKey(Endpoint, null=True, on_delete=models.SET_NULL)
    scenario = models.ForeignKey(Scenario, null=True, on_delete=models.SET_NULL)
    request_method = models.CharField(max_length=10)
    request_path = models.CharField(max_length=255, db_index=True)
    request_body = models.TextField(blank=True)
    response_status = models.IntegerField()
    response_body = models.TextField(blank=True)
    delay_applied_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mockvendor_calllog"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.request_method} {self.request_path} -> {self.response_status}"
