"""Run flow (design §6.3): seed -> trigger -> verify -> cleanup, per case row."""
from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import time
from dataclasses import dataclass, field

import requests

from . import schema, verifiers


def _aut_db_host() -> str:
    host = os.getenv("DB_HOST", "")
    port = os.getenv("DB_PORT", "5432")
    return f"{host}:{port}" if host else ""


def _aut_db_name() -> str:
    return os.getenv("AUT_DB_NAME", "")


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    # Per-call transcript captured while driving the AUT: one entry per request
    # actually sent (initial call + each follow-up step), so the dashboard can
    # show what the AUT returned. Bodies are truncated — this is for eyeballing,
    # not archival.
    responses: list = field(default_factory=list)


_MAX_BODY_CHARS = 8000


def _capture(label: str, method: str, url: str, resp) -> dict:
    """Snapshot a response for display. Never raises — capture is best-effort."""
    entry = {"label": label, "method": method, "url": url,
             "status": getattr(resp, "status_code", None), "body": "", "truncated": False}
    try:
        text = resp.text or ""
    except Exception as exc:
        entry["body"] = f"<could not read body: {exc}>"
        return entry
    try:  # pretty-print JSON when possible; fall back to raw text
        text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except Exception:
        pass
    if len(text) > _MAX_BODY_CHARS:
        entry["body"] = text[:_MAX_BODY_CHARS]
        entry["truncated"] = True
    else:
        entry["body"] = text
    return entry


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
        # Namespace this seed into a specific caller's scenario set (mockvendor
        # matcher.select: header > caller IP > default). "" = default set,
        # served to any caller with no isolated set of its own.
        "run_id": s.run_id,
        "responses": responses,
    }


class _ProducedAck:
    """Stand-in for a requests.Response on Kafka-driven cases.

    A produce has no HTTP status to assert, so resp.* checks are skipped for these
    cases (see evaluate). Verification happens entirely through the AUT's database
    and the mock CallLog, which is the right surface for an async consumer anyway.
    """
    status_code = None
    text = ""

    def __init__(self, topic: str, count: int):
        self.topic = topic
        self.count = count
        self.text = f"produced {count} message(s) to {topic}"

    def json(self):
        return {"produced": self.count, "topic": self.topic}


class Runner:
    def __init__(self, mock_base: str, aut_sqlite: str | None = None,
                 enable_kafka: bool = False, kafka_bootstrap: str | None = None):
        self.mock_base = mock_base.rstrip("/")
        self.aut_sqlite = aut_sqlite
        self.enable_kafka = enable_kafka
        self.kafka_bootstrap = kafka_bootstrap
        self._producer = None

    # --- kafka drive ---
    @staticmethod
    def _bootstrap_servers(bootstrap: str) -> list[str]:
        """``kafka://host:9092,host2:9092`` -> ``['host:9092', 'host2:9092']``.

        Broker lists are usually copied from an app config that carries a scheme
        (faust wants ``kafka://``); kafka-python wants bare ``host:port`` and fails
        obscurely on the scheme. Strip it here so either form can be pasted in.
        """
        out = []
        for b in bootstrap.split(","):
            b = b.strip()
            if not b:
                continue
            if "://" in b:
                b = b.split("://", 1)[1]
            out.append(b)
        return out

    def _kafka_producer(self, bootstrap: str):
        """One producer per runner, built on first use."""
        if self._producer is None:
            from kafka import KafkaProducer  # kafka-python
            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap_servers(bootstrap),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                # Wait for the broker so a produce failure surfaces as a test error
                # rather than a silently missing message.
                acks="all",
                retries=3,
            )
        return self._producer

    def _produce_once(self, case: schema.Case, corr_value: str):
        flat = dict(case.produce.body)
        # Same correlation-id injection as the HTTP drive: overwrite only a slot the
        # case actually declares, so replays and distinct-id runs address the right
        # event without fabricating fields the AUT does not expect.
        if case.id_key:
            key = next((k for k in flat if k == case.id_key or k.endswith("." + case.id_key)), None)
            if key is not None:
                flat[key] = corr_value
        body = _unflatten(flat)
        bootstrap = case.kafka_bootstrap or self.kafka_bootstrap
        if not bootstrap:
            raise RuntimeError(
                "produce.topic set but no kafka bootstrap; pass --kafka-bootstrap "
                "or set kafka.bootstrap on the case")
        print(f"[produce] {case.produce.topic} key={case.produce.key or corr_value}")
        print(f"[produce] payload: {body}")
        producer = self._kafka_producer(bootstrap)
        producer.send(case.produce.topic, key=case.produce.key or corr_value, value=body)
        return body

    def _drive_kafka(self, case: schema.Case) -> _ProducedAck:
        rep = case.repeat
        sent = 0
        for n in range(rep["distinct_ids"]):
            cid = case.flow_id if rep["distinct_ids"] == 1 else f"{case.flow_id}-{n}"
            total = max(rep["same_flow_id"], rep["concurrent"])
            if rep["concurrent"] > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=rep["concurrent"]) as ex:
                    futs = [ex.submit(self._produce_once, case, cid) for _ in range(rep["concurrent"])]
                    for f in concurrent.futures.as_completed(futs):
                        f.result()
                        sent += 1
            else:
                for _ in range(total):
                    self._produce_once(case, cid)
                    sent += 1
        # Block until the broker has them all, so db.delay_ms measures the AUT's
        # processing time and not our own send buffer draining.
        self._producer.flush(timeout=30)
        print(f"[produce] flushed {sent} message(s)")
        return _ProducedAck(case.produce.topic, sent)

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
    def _drive_once(self, case: schema.Case, corr_value: str) -> requests.Response:
        flat = dict(case.call["body"])
        # Inject the runtime correlation id into whichever body key holds it
        # (top-level or nested, e.g. data.flow_id). The key is configurable via
        # case.id_key (default "flow_id" for UKS); id_key="" disables injection.
        # Only overwrite a slot the case actually declares — never fabricate one,
        # so AUTs without this concept aren't sent a spurious field.
        if case.id_key:
            key = next((k for k in flat if k == case.id_key or k.endswith("." + case.id_key)), None)
            if key is not None:
                flat[key] = corr_value
        body = _unflatten(flat)
        print(f"[call] {case.call['method']} {case.call['url']}")
        print(f"[call] payload: {body}")
        return requests.request(case.call["method"], case.call["url"],
                                json=body, headers=case.call["headers"], timeout=30)

    def drive(self, case: schema.Case):
        # Consumer-driven AUT: produce to Kafka and let its own consumer pick it up.
        if case.produce and case.produce.topic:
            return self._drive_kafka(case)

        rep = case.repeat
        chosen = None
        for n in range(rep["distinct_ids"]):
            fid = case.flow_id if rep["distinct_ids"] == 1 else f"{case.flow_id}-{n}"
            # same_flow_id replays + concurrency bombardment
            total = max(rep["same_flow_id"], rep["concurrent"])
            if rep["concurrent"] > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=rep["concurrent"]) as ex:
                    futs = [ex.submit(self._drive_once, case, fid) for _ in range(rep["concurrent"])]
                    for f in concurrent.futures.as_completed(futs):
                        resp = f.result()
                        if chosen is None or (
                            resp.status_code == 200 and chosen.status_code != 200
                        ):
                            chosen = resp
            else:
                for _ in range(total):
                    resp = self._drive_once(case, fid)
                    if chosen is None or (
                        resp.status_code == 200 and chosen.status_code != 200
                    ):
                        chosen = resp
        return chosen

    # --- evaluate ---
    def evaluate(self, case: schema.Case, response,
                 call_baseline: dict | None = None) -> list[str]:
        errs: list[str] = []
        # Kafka-driven cases have no HTTP response; resp.* is not applicable to them.
        kafka_driven = isinstance(response, _ProducedAck)
        if kafka_driven and (case.resp["status"] is not None or case.resp["body"]):
            errs.append("resp.* cannot be asserted on a produce-driven case; "
                        "verify via dbN.* and calls instead")
        if not kafka_driven and case.resp["status"] is not None and response.status_code != case.resp["status"]:
            errs.append(f"resp.status expected {case.resp['status']}, got {response.status_code}")
        if case.resp["body"] and not kafka_driven:
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
                errs += verifiers.verify_db_postgres(
                    case.db_host or _aut_db_host(),
                    case.db_database or _aut_db_name(),
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
            # For distinct_ids > 1 the runner generates {corr}-0 … {corr}-N-1;
            # clean all of them regardless of what the db_check's where says.
            # Keyed on case.id_key (the configurable correlation column, default
            # "flow_id") so this is not UKS-specific.
            if rep["distinct_ids"] > 1 and case.id_key and case.id_key in chk.where:
                base = case.flow_id
                wheres = [{**chk.where, case.id_key: f"{base}-{n}"}
                          for n in range(rep["distinct_ids"])]
            for w in wheres:
                if self.aut_sqlite:
                    verifiers.cleanup_db_sqlite(self.aut_sqlite, chk.table, w)
                elif case.db_host or _aut_db_host():
                    verifiers.cleanup_db_postgres(
                        case.db_host or _aut_db_host(),
                        case.db_database or _aut_db_name(),
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
        responses: list = []
        response = self.drive(case)
        responses.append(_capture("call 1 · initial", case.call["method"], case.call["url"], response))
        for i, step in enumerate(case.call_steps):
            if step.get("delay_ms"):
                time.sleep(step["delay_ms"] / 1000)
            step_body = _unflatten(step["body"])
            print(f"[call_step] {step['method']} {step['url']}")
            print(f"[call_step] payload: {step_body}")
            step_resp = requests.request(
                step["method"], step["url"],
                json=step_body,
                headers=step["headers"],
                timeout=30,
            )
            responses.append(_capture(f"call {i + 2} · follow-up", step["method"], step["url"], step_resp))
            if step.get("expect_status") and step_resp.status_code != step["expect_status"]:
                return CaseResult(case.case_id, passed=False, responses=responses,
                                  errors=[f"call step {step['url']}: expected {step['expect_status']}, got {step_resp.status_code}"])
            response = step_resp
        if case.db_delay_ms:
            print(f"[verify] waiting {case.db_delay_ms} ms before DB/call verification...")
            time.sleep(case.db_delay_ms / 1000)
        errors = self.evaluate(case, response, call_baseline=baseline)
        return CaseResult(case.case_id, passed=not errors, errors=errors, responses=responses)


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
