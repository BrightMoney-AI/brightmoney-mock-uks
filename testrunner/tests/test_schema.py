"""Tests for the §12 CSV schema parser + MUST validation (no Django needed)."""
from __future__ import annotations

from testrunner import schema


def test_parse_map_and_escapes():
    assert schema.parse_map("a=1;b=2") == {"a": "1", "b": "2"}
    # escaped semicolon stays inside the value
    assert schema.parse_map(r"k=a\;b") == {"k": "a;b"}


def test_resp_cell_splits_reserved_and_body():
    r = schema._resp_from_cell("status=200;format=json;delay_ms=50;result=pass;n=3")
    assert r["status"] == 200 and r["format"] == "json" and r["delay_ms"] == 50
    assert r["canonical"] == {"result": "pass", "n": 3}


def _base_row(**over):
    row = {
        "case_id": "C1", "tags": "happy",
        "seed1.path": "/vendor/idology/verify", "seed1.scenario": "idology-pass",
        "seed1.match": "$.test_path=p", "seed1.resp": "status=200;summary_result=PASS",
        "call.url": "http://aut/enroll", "call.body.test_path": "p",
        "resp.status": "200", "resp.body": "decision=PASSED",
        "db.host": "h:5432", "db1.table": "enrollment", "db1.where": "flow_id=C1",
        "db1.expect": "decision=PASSED", "calls": "/vendor/idology/verify=1",
    }
    row.update(over)
    return row


def test_valid_case_has_no_violations():
    c = schema.parse_case(_base_row())
    assert schema.validate(c) == []
    assert c.seeds[0].match_key == "$.test_path" and c.seeds[0].match_value == "p"
    assert c.calls == {"/vendor/idology/verify": 1}


def test_missing_db_host_flagged():
    c = schema.parse_case(_base_row(**{"db.host": ""}))
    assert any("db.host" in e for e in schema.validate(c))


def test_duplicate_scenario_names_flagged():
    row = _base_row()
    row.update({"seed2.path": "/vendor/lexisnexis/verify", "seed2.scenario": "idology-pass",
                "seed2.resp": "status=200;result=PASS"})
    c = schema.parse_case(row)
    assert any("unique" in e for e in schema.validate(c))


def test_raw_and_body_conflict_flagged():
    c = schema.parse_case(_base_row(**{"seed1.resp": "status=200;raw=xx;result=pass"}))
    assert any("raw OR body" in e for e in schema.validate(c))


def test_shared_endpoint_needs_disambiguation():
    row = _base_row()
    row.update({"seed2.path": "/vendor/idology/verify", "seed2.scenario": "dup",
                "seed2.match": "", "seed2.resp": "status=200;summary_result=FAIL"})
    # seed1 has a match, seed2 has none and priority 0 -> ambiguous
    row["seed1.match"] = ""
    c = schema.parse_case(row)
    assert any("match or priority" in e for e in schema.validate(c))
