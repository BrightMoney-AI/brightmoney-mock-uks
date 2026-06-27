"""Run flow (design §6.3): seed -> trigger -> verify -> cleanup, per case row."""
from __future__ import annotations

import concurrent.futures
import csv
from dataclasses import dataclass, field

import requests

from . import schema, verifiers


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    skipped: bool = False


def _seed_payload(s: schema.SeedGroup) -> dict:
    resp = {"status": s.resp.get("status", 200)}
    if "format" in s.resp:
        resp["format"] = s.resp["format"]
    if "delay_ms" in s.resp:
        resp["delay_ms"] = s.resp["delay_ms"]
    if s.resp.get("raw"):
        resp["raw"] = s.resp["raw"]
    else:
        resp["canonical"] = s.resp.get("canonical", {})
    return {
        "method": s.method, "path": s.path, "scenario": s.scenario,
        "priority": s.priority, "is_sequence": s.is_sequence,
        "match_key": s.match_key, "match_value": s.match_value,
        "responses": [resp],
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
        body = dict(case.call["body"])
        body["flow_id"] = flow_id
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
    def evaluate(self, case: schema.Case, response: requests.Response) -> list[str]:
        errs: list[str] = []
        if case.resp["status"] is not None and response.status_code != case.resp["status"]:
            errs.append(f"resp.status expected {case.resp['status']}, got {response.status_code}")
        if case.resp["body"]:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            for k, v in case.resp["body"].items():
                if not verifiers._expect_match(payload.get(k), v):
                    errs.append(f"resp.body.{k} expected {v!r}, got {payload.get(k)!r}")

        for chk in case.db_checks:
            if self.aut_sqlite:
                errs += verifiers.verify_db_sqlite(self.aut_sqlite, chk.table, chk.where, chk.expect)
            else:
                errs += verifiers.verify_db_postgres(case.db_host, case.db_database,
                                                     chk.table, chk.where, chk.expect)

        if case.calls:
            errs += verifiers.verify_calls(self.mock_base, case.calls)

        if self.enable_kafka:
            for kc in case.kafka_checks:
                errs += verifiers.verify_kafka(case.kafka_bootstrap, kc.topic, kc.key, kc.expect)
        return errs

    # --- AUT DB cleanup ---
    def _cleanup_aut_db(self, case: schema.Case) -> None:
        """Delete AUT rows matching each db_check's where clause before the test.

        Prevents the AUT's idempotency guard from returning stale results from a
        previous run with the same flow_id.
        """
        for chk in case.db_checks:
            if not chk.where:
                continue
            if self.aut_sqlite:
                verifiers.cleanup_db_sqlite(self.aut_sqlite, chk.table, chk.where)
            elif case.db_host:
                verifiers.cleanup_db_postgres(case.db_host, case.db_database,
                                              chk.table, chk.where)

    # --- one case end to end ---
    def run_case(self, case: schema.Case) -> CaseResult:
        violations = schema.validate(case)
        if violations:
            return CaseResult(case.case_id, passed=False, errors=[f"MUST: {v}" for v in violations])
        # Full reset at the start: clears scenarios from the previous case AND its
        # CallLog so call-count assertions only see calls made in this case.
        self.reset()
        # Remove any AUT DB rows from a prior run with the same flow_id so the
        # AUT's idempotency check doesn't return a cached result and skip the
        # vendor calls entirely.
        self._cleanup_aut_db(case)
        try:
            self.seed(case)
            response = self.drive(case)
            errors = self.evaluate(case, response)
            return CaseResult(case.case_id, passed=not errors, errors=errors)
        finally:
            # Clear only seeded scenarios; keep CallLog so failures are inspectable
            # via GET /mock/admin/calls without losing evidence.
            self.reset_scenarios()


def load_cases(csv_path: str) -> list[schema.Case]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return [schema.parse_case(row) for row in csv.DictReader(f)]
