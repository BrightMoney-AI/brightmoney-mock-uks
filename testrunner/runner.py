"""Run flow (design §6.3): seed -> trigger -> verify -> cleanup, per case row."""
from __future__ import annotations

import concurrent.futures
import csv
import time
from dataclasses import dataclass, field

import requests

from . import schema, verifiers


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    skipped: bool = False


def _one_response(src: dict) -> dict:
    resp = {"status": src.get("status", 200)}
    if "format" in src:
        resp["format"] = src["format"]
    if "delay_ms" in src:
        resp["delay_ms"] = src["delay_ms"]
    if src.get("raw"):
        resp["raw"] = src["raw"]
    else:
        resp["canonical"] = src.get("canonical", {})
    return resp


def _dig(payload, dotted_key: str):
    """Resolve a dotted key against a nested JSON payload: 'error.error_code'.

    Falls back to a flat lookup when the literal key exists (e.g. 'flow_id').
    """
    if isinstance(payload, dict) and dotted_key in payload:
        return payload[dotted_key]
    cur = payload
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _unflatten(flat: dict) -> dict:
    """Expand dot-separated keys into nested dicts: {'a.b': v} -> {'a': {'b': v}}."""
    out = {}
    for key, val in flat.items():
        parts = key.split(".")
        d = out
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = val
    return out


def _seed_payload(s: schema.SeedGroup) -> dict:
    responses = [_one_response(r) for r in s.responses] or [_one_response({})]
    return {
        "method": s.method, "path": s.path, "scenario": s.scenario,
        "priority": s.priority, "is_sequence": s.is_sequence or len(responses) > 1,
        "match_key": s.match_key, "match_value": s.match_value,
        "responses": responses,
    }


class Runner:
    def __init__(self, mock_base: str, aut_sqlite: str | None = None,
                 enable_kafka: bool = False):
        self.mock_base = mock_base.rstrip("/")
        self.aut_sqlite = aut_sqlite
        self.enable_kafka = enable_kafka

    # --- mock admin helpers ---
    def reset(self):
        """Full reset: clears scenarios + CallLog. Used at the start of each case."""
        requests.post(self.mock_base + "/mock/admin/reset", json={}, timeout=10)

    def reset_scenarios(self):
        """Clears only seeded Scenarios; preserves CallLog for post-run inspection."""
        requests.post(self.mock_base + "/mock/admin/reset/scenarios", json={}, timeout=10)

    def seed(self, case: schema.Case):
        for s in case.seeds:
            r = requests.post(self.mock_base + "/mock/admin/scenarios",
                              json=_seed_payload(s), timeout=10)
            r.raise_for_status()

    # --- drive ---
    def _drive_once(self, case: schema.Case, flow_id: str) -> requests.Response:
        flat = dict(case.call["body"])
        # Inject runtime flow_id into whichever key holds it (top-level or nested e.g. data.flow_id)
        flow_id_key = next(
            (k for k in flat if k == "flow_id" or k.endswith(".flow_id")), "flow_id"
        )
        flat[flow_id_key] = flow_id
        body = _unflatten(flat)
        return requests.request(case.call["method"], case.call["url"],
                                json=body, headers=case.call["headers"], timeout=30)

    def drive(self, case: schema.Case) -> requests.Response:
        rep = case.repeat
        last = None
        for n in range(rep["distinct_ids"]):
            fid = case.flow_id if rep["distinct_ids"] == 1 else f"{case.flow_id}-{n}"
            # same_flow_id replays + concurrency bombardment
            total = max(rep["same_flow_id"], rep["concurrent"])
            if rep["concurrent"] > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=rep["concurrent"]) as ex:
                    futs = [ex.submit(self._drive_once, case, fid) for _ in range(rep["concurrent"])]
                    for f in concurrent.futures.as_completed(futs):
                        last = f.result()
            else:
                for _ in range(total):
                    last = self._drive_once(case, fid)
        return last

    # --- evaluate ---
    def evaluate(self, case: schema.Case, response: requests.Response,
                 call_baseline: dict | None = None) -> list[str]:
        errs: list[str] = []
        if case.resp["status"] is not None and response.status_code != case.resp["status"]:
            errs.append(f"resp.status expected {case.resp['status']}, got {response.status_code}")
        if case.resp["body"]:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            for k, v in case.resp["body"].items():
                if not verifiers._expect_match(_dig(payload, k), v):
                    errs.append(f"resp.body.{k} expected {v!r}, got {_dig(payload, k)!r}")

        for chk in case.db_checks:
            if self.aut_sqlite:
                errs += verifiers.verify_db_sqlite(self.aut_sqlite, chk.table, chk.where, chk.expect)
            else:
                errs += verifiers.verify_db_postgres(case.db_host, case.db_database,
                                                     chk.table, chk.where, chk.expect)

        if case.calls:
            errs += verifiers.verify_calls(self.mock_base, case.calls, baseline=call_baseline)

        if self.enable_kafka:
            for kc in case.kafka_checks:
                errs += verifiers.verify_kafka(case.kafka_bootstrap, kc.topic, kc.key, kc.expect)
        return errs

    # --- AUT DB cleanup ---
    def _cleanup_aut_db(self, case: schema.Case) -> None:
        """Delete AUT rows matching each db_check's where clause before the test.

        Prevents the AUT's idempotency guard from returning stale results from a
        previous run with the same flow_id.  For multi-flow cases (distinct_ids > 1)
        all generated flow_id-N variants are cleaned so none are cached.
        """
        rep = case.repeat
        for chk in case.db_checks:
            if not chk.where:
                continue
            wheres = [chk.where]
            # For distinct_ids > 1 the runner generates {case.flow_id}-0 …
            # {case.flow_id}-N-1; clean all of them regardless of what the
            # db_check's where clause says.
            if rep["distinct_ids"] > 1 and "flow_id" in chk.where:
                base = case.flow_id
                wheres = [{**chk.where, "flow_id": f"{base}-{n}"}
                          for n in range(rep["distinct_ids"])]
            for w in wheres:
                if self.aut_sqlite:
                    verifiers.cleanup_db_sqlite(self.aut_sqlite, chk.table, w)
                elif case.db_host:
                    verifiers.cleanup_db_postgres(case.db_host, case.db_database,
                                                  chk.table, w)

    # --- call log snapshot ---
    def _get_call_counts(self) -> dict:
        try:
            resp = requests.get(self.mock_base + "/mock/admin/calls", timeout=10)
            return resp.json().get("counts", {})
        except Exception:
            return {}

    # --- one case end to end ---
    def run_case(self, case: schema.Case) -> CaseResult:
        violations = schema.validate(case)
        if violations:
            return CaseResult(case.case_id, passed=False, errors=[f"MUST: {v}" for v in violations])
        # Clean BEFORE: clear scenarios from the previous case only.
        # CallLog is NEVER cleared — call counts are delta'd against a pre-test
        # baseline so assertions only see calls made in this test.
        self.reset_scenarios()
        # Remove any AUT DB rows from a prior run with the same flow_id so the
        # AUT's idempotency check doesn't return a cached result and skip the
        # vendor calls entirely.
        self._cleanup_aut_db(case)
        # Snapshot call counts BEFORE seeding/driving so the delta is accurate.
        baseline = self._get_call_counts()
        self.seed(case)
        response = self.drive(case)
        for step in case.call_steps:
            if step.get("delay_ms"):
                time.sleep(step["delay_ms"] / 1000)
            step_resp = requests.request(
                step["method"], step["url"],
                json=_unflatten(step["body"]),
                headers=step["headers"],
                timeout=30,
            )
            if step.get("expect_status") and step_resp.status_code != step["expect_status"]:
                return CaseResult(case.case_id, passed=False,
                                  errors=[f"call step {step['url']}: expected {step['expect_status']}, got {step_resp.status_code}"])
        errors = self.evaluate(case, response, call_baseline=baseline)
        return CaseResult(case.case_id, passed=not errors, errors=errors)


def load_cases(csv_path: str) -> list[schema.Case]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if schema.is_new_format(fieldnames):
        # Group rows by case_id (preserving order); all rows for a case share seeds.
        groups: dict[str, list[dict]] = {}
        for row in rows:
            cid = (row.get("case_id") or "").strip()
            if not cid:
                continue
            groups.setdefault(cid, []).append(row)
        return [schema.parse_case_new(grp) for grp in groups.values()]

    return [schema.parse_case(row) for row in rows]
