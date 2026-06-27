"""Tests for the mock server core: matcher, discriminator, sequence, delay,
serializers, and the admin API. Uses Django's test client (no live server)."""
from __future__ import annotations

import json
import time

import pytest
from django.test import Client

from mockvendor import seed
from mockvendor.models import CallLog, Scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return Client()


def _seed(**kw):
    return seed.seed_scenario_dict(kw)


def test_happy_json_served():
    _seed(path="/vendor/idology/verify", scenario="idology-pass",
          responses=[{"status": 200, "canonical": {"summary_result": "PASS"}}])
    c = Client()
    r = c.post("/vendor/idology/verify", data="{}", content_type="application/json")
    assert r.status_code == 200
    assert json.loads(r.content)["summary_result"] == "PASS"


def test_discriminator_picks_scenario():
    _seed(path="/vendor/idology/verify", scenario="to-pass", match_key="$.test_path",
          match_value="p", responses=[{"status": 200, "canonical": {"r": "PASS"}}])
    _seed(path="/vendor/idology/verify", scenario="to-fail", match_key="$.test_path",
          match_value="f", responses=[{"status": 200, "canonical": {"r": "FAIL"}}])
    c = Client()
    r = c.post("/vendor/idology/verify", data=json.dumps({"test_path": "f"}),
               content_type="application/json")
    assert json.loads(r.content)["r"] == "FAIL"


def test_priority_breaks_tie():
    _seed(path="/x", scenario="low", priority=1, responses=[{"status": 200, "canonical": {"w": "low"}}])
    _seed(path="/x", scenario="high", priority=9, responses=[{"status": 200, "canonical": {"w": "high"}}])
    c = Client()
    r = c.post("/x", data="{}", content_type="application/json")
    assert json.loads(r.content)["w"] == "high"


def test_sequence_advances():
    _seed(path="/poll", scenario="poll", is_sequence=True, responses=[
        {"status": 200, "canonical": {"state": "PENDING"}},
        {"status": 200, "canonical": {"state": "PENDING"}},
        {"status": 200, "canonical": {"state": "COMPLETED"}},
    ])
    c = Client()
    states = [json.loads(c.post("/poll", data="{}", content_type="application/json").content)["state"]
              for _ in range(4)]
    assert states == ["PENDING", "PENDING", "COMPLETED", "COMPLETED"]


def test_delay_applied():
    _seed(path="/slow", scenario="slow",
          responses=[{"status": 200, "canonical": {"ok": True}, "delay_ms": 300}])
    c = Client()
    t0 = time.time()
    c.post("/slow", data="{}", content_type="application/json")
    assert time.time() - t0 >= 0.28


def test_xml_format():
    _seed(path="/xmlep", scenario="x",
          responses=[{"status": 200, "format": "xml", "canonical": {"a": "b"}}])
    c = Client()
    r = c.post("/xmlep", data="{}", content_type="application/json")
    assert r["Content-Type"].startswith("application/xml")
    assert b"<a>b</a>" in r.content


def test_raw_override_malformed():
    _seed(path="/broken", scenario="b",
          responses=[{"status": 200, "raw": '{"summary-result":{"key":"id.suc'}])
    c = Client()
    r = c.post("/broken", data="{}", content_type="application/json")
    assert r.content == b'{"summary-result":{"key":"id.suc'


def test_error_status_code():
    _seed(path="/err", scenario="e",
          responses=[{"status": 500, "canonical": {"error": "boom"}}])
    c = Client()
    r = c.post("/err", data="{}", content_type="application/json")
    assert r.status_code == 500


def test_no_endpoint_404_and_logged():
    c = Client()
    r = c.post("/nope", data="{}", content_type="application/json")
    assert r.status_code == 404
    assert CallLog.objects.filter(request_path="/nope", response_status=404).exists()


def test_calllog_written():
    _seed(path="/logme", scenario="l", responses=[{"status": 200, "canonical": {}}])
    c = Client()
    c.post("/logme", data="{}", content_type="application/json")
    assert CallLog.objects.filter(request_path="/logme", response_status=200).count() == 1


def test_admin_api_seed_and_reset():
    c = Client()
    payload = {"path": "/api-seed", "scenario": "s",
               "responses": [{"status": 200, "canonical": {"ok": True}}]}
    r = c.post("/mock/admin/scenarios", data=json.dumps(payload), content_type="application/json")
    assert r.status_code == 201
    assert Scenario.objects.filter(name="s").exists()
    r2 = c.post("/mock/admin/reset", data="{}", content_type="application/json")
    assert r2.status_code == 200
    assert not Scenario.objects.exists()


def test_admin_calls_counts():
    _seed(path="/counted", scenario="c", responses=[{"status": 200, "canonical": {}}])
    c = Client()
    c.post("/counted", data="{}", content_type="application/json")
    c.post("/counted", data="{}", content_type="application/json")
    r = c.get("/mock/admin/calls")
    assert r.json()["counts"]["/counted"] == 2
