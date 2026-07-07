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


class TestCase(models.Model):
    """A structured, DB-stored test case editable from the dashboard.

    ``definition`` mirrors the runner's parsed Case (seeds / call / call_steps /
    expectations), so it runs through the same testrunner.Runner via
    schema.case_from_dict — no CSV round-trip. Existing CSV suites can be
    imported into these rows to become visually editable.
    """

    case_id = models.CharField(max_length=120)
    suite = models.CharField(max_length=120, blank=True, db_index=True)  # grouping label
    definition = models.JSONField(default=dict)  # {seeds, call, call_steps, resp, calls, db_checks, ...}
    tags = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mockvendor_testcase"
        ordering = ["suite", "case_id"]
        unique_together = ("suite", "case_id")

    def __str__(self) -> str:
        return f"{self.suite}:{self.case_id}" if self.suite else self.case_id


class TestRun(models.Model):
    """One execution of a CSV test suite (design: dashboard-triggered runs).

    Created ``pending`` by the API, driven to a terminal state by the
    ``run_testsuite`` management command running as a detached subprocess so the
    run survives gunicorn worker recycling. The row is the source of truth for
    progress; the dashboard polls it.
    """

    STATUS = [
        ("pending", "pending"), ("running", "running"),
        ("passed", "passed"), ("failed", "failed"),
        ("error", "error"), ("cancelled", "cancelled"),
    ]

    source = models.CharField(max_length=8, default="csv")  # "csv" | "db"
    csv_path = models.CharField(max_length=255, blank=True)  # relative to data/ (csv source)
    suite = models.CharField(max_length=120, blank=True)     # TestCase suite (db source)
    case_ids = models.JSONField(default=list, blank=True)    # explicit TestCase ids (db source)
    tag = models.CharField(max_length=120, blank=True)   # optional tag filter
    case_filter = models.TextField(blank=True)           # optional comma-sep case_ids
    mock_base = models.CharField(max_length=255, default="http://127.0.0.1")
    status = models.CharField(max_length=16, choices=STATUS, default="pending", db_index=True)
    total = models.IntegerField(default=0)
    passed = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)
    error = models.TextField(blank=True)                 # fatal error if the run itself blew up
    log_path = models.CharField(max_length=255, blank=True)
    pid = models.IntegerField(null=True, blank=True)
    created_ip = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mockvendor_testrun"
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"run<{self.id} {self.csv_path} {self.status}>"


class TestResult(models.Model):
    """Per-case outcome within a TestRun."""

    run = models.ForeignKey(TestRun, related_name="results", on_delete=models.CASCADE)
    case_id = models.CharField(max_length=120, db_index=True)
    passed = models.BooleanField(default=False)
    skipped = models.BooleanField(default=False)
    errors = models.JSONField(default=list, blank=True)
    # Per-call transcript captured by the runner (initial call + each step):
    # [{label, method, url, status, body, truncated}]. Bodies are truncated.
    responses = models.JSONField(default=list, blank=True)
    duration_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mockvendor_testresult"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"result<{self.run_id}:{self.case_id} {'PASS' if self.passed else 'FAIL'}>"


class CallLog(models.Model):
    """Append-only record of every received request (design §4.3). shouldn't be reset in any call or migration"""

    endpoint = models.ForeignKey(Endpoint, null=True, on_delete=models.SET_NULL)
    scenario = models.ForeignKey(Scenario, null=True, on_delete=models.SET_NULL)
    request_method = models.CharField(max_length=10)
    request_path = models.CharField(max_length=255, db_index=True)
    # Caller identity used for parallel-run scenario isolation (X-Forwarded-For
    # first hop, else REMOTE_ADDR). "" when unknown. See matcher.select.
    request_ip = models.CharField(max_length=64, blank=True, db_index=True)
    # The run_id namespace the matched scenario belonged to ("" = default set).
    matched_run_id = models.CharField(max_length=120, blank=True)
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
