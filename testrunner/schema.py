"""Parse + validate a case row against the §12 CSV rule set.

In-cell syntax (§12.1): a map is ``key=value`` pairs joined by ``;``; a list is
values joined by ``;``. A literal ``;`` or ``,`` inside a value is backslash
escaped. ``not_null`` and ``/regex/`` are allowed on the right of an expect pair.
Empty cells mean "not set".
"""
from __future__ import annotations

import re
import uuid as _uuid
from dataclasses import dataclass, field

# Reserved keys inside a seedN.resp cell; everything else is a canonical body field.
_RESP_RESERVED = {"status", "format", "delay_ms", "raw"}

_UUID_PATTERN = re.compile(r'\{\{uuid(?::([^}]+))?\}\}')


def _interpolate(text: str, ctx: dict) -> str:
    """Replace {{uuid}} with a fresh UUID4, {{uuid:name}} with a named UUID (same value reused within a case)."""
    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name:
            if name not in ctx:
                ctx[name] = str(_uuid.uuid4())
            return ctx[name]
        return str(_uuid.uuid4())
    return _UUID_PATTERN.sub(_sub, text)


class ValidationError(Exception):
    pass


def split_escaped(cell: str, sep: str = ";") -> list[str]:
    """Split on *sep* but honour ``\\;`` / ``\\,`` escapes."""
    out, buf, i = [], [], 0
    while i < len(cell):
        ch = cell[i]
        if ch == "\\" and i + 1 < len(cell) and cell[i + 1] in ";,":
            buf.append(cell[i + 1])
            i += 2
            continue
        if ch == sep:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s for s in (x.strip() for x in out) if s != ""]


def parse_map(cell: str) -> dict:
    """``a=1;b=2`` -> {'a': '1', 'b': '2'}."""
    result: dict[str, str] = {}
    for pair in split_escaped(cell):
        if "=" not in pair:
            raise ValidationError(f"map cell missing '=': {pair!r}")
        k, v = pair.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def parse_calls_cell(cell: str) -> dict:
    """Parse ``calls`` column; values may be exact counts (``1``) or minimums (``>=1``).

    LN OAuth token fetches are cached by UKS and must not be asserted — drop them
    if present so stale CSV rows cannot fail on ``expected 1, got 0``.
    """
    _SKIP_CALL_PATHS = ("/LN.WebServices/api/OAuth2/Token",)
    result: dict = {}
    for pair in split_escaped(cell):
        pair = pair.strip()
        m = re.match(r"^(.+)>=(\d+)$", pair)
        if m:
            path = m.group(1).strip()
            if path in _SKIP_CALL_PATHS:
                continue
            result[path] = f">={m.group(2)}"
            continue
        m = re.match(r"^(.+)=(\d+)$", pair)
        if not m:
            raise ValidationError(f"calls pair invalid: {pair!r}")
        path = m.group(1).strip()
        if path in _SKIP_CALL_PATHS:
            continue
        result[path] = int(m.group(2))
    return result


def _parse_db_checks(g) -> list[DbCheck]:
    """Parse db1..db10 checks; empty slots (e.g. db2 blank, db3 set) are skipped."""
    db_checks: list[DbCheck] = []
    for j in range(1, 11):
        if not g(f"db{j}.table"):
            continue
        db_checks.append(DbCheck(
            table=g(f"db{j}.table"),
            where=parse_map(g(f"db{j}.where")) if g(f"db{j}.where") else {},
            expect=parse_map(g(f"db{j}.expect")) if g(f"db{j}.expect") else {},
        ))
    return db_checks


@dataclass
class SeedGroup:
    index: int
    path: str
    method: str
    scenario: str
    priority: int
    match_key: str
    match_value: str
    is_sequence: bool
    responses: list  # ordered list of resp dicts {status, format, delay_ms, raw, canonical}
    # Scenario-set namespace this seed is written into (mockvendor.matcher.select):
    # "" = default (served to any caller with no per-host set of its own);
    # a caller IP / run_id = isolated to that host, see the parallel-run docs.
    run_id: str = ""

    @property
    def resp(self) -> dict:
        """First response — back-compat for single-response validation/seeding."""
        return self.responses[0] if self.responses else {}


@dataclass
class DbCheck:
    table: str
    where: dict
    expect: dict


@dataclass
class KafkaCheck:
    topic: str
    key: str
    expect: dict | str  # dict, or literal "absent"


@dataclass
class Case:
    case_id: str
    flow_id: str  # correlation/idempotency VALUE (historically the UKS flow id)
    tags: list[str]
    client_context: str
    seeds: list[SeedGroup]
    call: dict  # {method,url,headers:{},body:{},expect_status}
    repeat: dict  # {same_flow_id,distinct_ids,concurrent}
    resp: dict  # {status, body:{}}
    db_host: str
    db_database: str
    db_checks: list[DbCheck]
    kafka_bootstrap: str
    kafka_checks: list[KafkaCheck]
    calls: dict  # {path: count}
    call_steps: list = field(default_factory=list)  # [{method,url,headers,body,expect_status,delay_ms}]
    db_delay_ms: int = 0  # ms to wait before DB/call-count verification (avoids race conditions)
    notes: str = ""
    # Body path of the correlation/idempotency id the runner injects + replays +
    # cleans by. Default "flow_id" (UKS); set to your AUT's key (e.g. "order_id")
    # or "" to disable id injection entirely for AUTs that have no such concept.
    id_key: str = "flow_id"
    raw: dict = field(default_factory=dict)


def _resp_from_cell(cell: str) -> dict:
    m = parse_map(cell)
    resp = {"status": int(m.get("status", 200)), "canonical": {}}
    if "format" in m:
        resp["format"] = m["format"]
    if "delay_ms" in m:
        resp["delay_ms"] = int(m["delay_ms"])
    if "raw" in m:
        resp["raw"] = m["raw"]
    for k, v in m.items():
        if k not in _RESP_RESERVED:
            resp["canonical"][_coerce_key(k)] = _coerce(v)
    return resp


def _coerce_key(k: str) -> str:
    return k


def _coerce(v: str):
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1]  # single-quoted → force string, no coercion
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _parse_extra_calls(first: dict, g, _ctx: dict, interp=True) -> list:
    """Parse call2.*, call3.*, … into sequential call steps."""
    _i = (lambda s: _interpolate(s, _ctx)) if interp else (lambda s: s)
    steps = []
    for n in range(2, 10):
        if not g(f"call{n}.url"):
            continue
        body = {k[len(f"call{n}.body."):]: _coerce(_i(v.strip()))
                for k, v in first.items() if k.startswith(f"call{n}.body.") and (v or "").strip()}
        steps.append({
            "method": g(f"call{n}.method") or "POST",
            "url": g(f"call{n}.url"),
            "headers": parse_map(g(f"call{n}.headers")) if g(f"call{n}.headers") else {},
            "body": body,
            "expect_status": int(g(f"call{n}.expect_status")) if g(f"call{n}.expect_status") else None,
            "delay_ms": int(g(f"call{n}.delay_ms")) if g(f"call{n}.delay_ms") else 0,
        })
    return steps


def parse_case(row: dict) -> Case:
    _ctx: dict = {}
    g = lambda k: _interpolate((row.get(k) or "").strip(), _ctx)

    # seed groups, numbered contiguously from 1
    seeds: list[SeedGroup] = []
    i = 1
    while g(f"seed{i}.path"):
        match_cell = g(f"seed{i}.match")
        mk, mv = "", ""
        if match_cell:
            mm = parse_map(match_cell)
            (mk, mv), = mm.items() if len(mm) == 1 else [(list(mm)[0], mm[list(mm)[0]])]
        seeds.append(SeedGroup(
            index=i, path=g(f"seed{i}.path"),
            method=g(f"seed{i}.method") or "POST",
            scenario=g(f"seed{i}.scenario"),
            priority=int(g(f"seed{i}.priority") or 0),
            match_key=mk, match_value=mv,
            is_sequence=g(f"seed{i}.is_sequence").lower() in ("1", "true", "yes"),
            responses=[_resp_from_cell(g(f"seed{i}.resp"))] if g(f"seed{i}.resp") else [],
        ))
        i += 1

    # call.body.* columns
    body = {k[len("call.body."):]: _coerce(_interpolate(v.strip(), _ctx))
            for k, v in row.items() if k.startswith("call.body.") and (v or "").strip()}
    call = {
        "method": g("call.method") or "POST",
        "url": g("call.url"),
        "headers": parse_map(g("call.headers")) if g("call.headers") else {},
        "body": body,
        "expect_status": int(g("call.expect_status")) if g("call.expect_status") else None,
    }
    repeat = {
        "same_flow_id": int(g("repeat.same_flow_id") or 1),
        "distinct_ids": int(g("repeat.distinct_ids") or 1),
        "concurrent": int(g("repeat.concurrent") or 1),
    }
    resp = {
        "status": int(g("resp.status")) if g("resp.status") else None,
        "body": parse_map(g("resp.body")) if g("resp.body") else {},
    }

    db_checks = _parse_db_checks(g)

    # kafkaN.* checks
    kafka_checks: list[KafkaCheck] = []
    k = 1
    while g(f"kafka{k}.topic"):
        exp_cell = g(f"kafka{k}.expect")
        expect: dict | str = "absent" if exp_cell == "absent" else parse_map(exp_cell)
        kafka_checks.append(KafkaCheck(topic=g(f"kafka{k}.topic"), key=g(f"kafka{k}.key"), expect=expect))
        k += 1

    calls = parse_calls_cell(g("calls")) if g("calls") else {}

    return Case(
        case_id=g("case_id"), flow_id=g("flow_id") or g("case_id"),
        tags=split_escaped(g("tags")) if g("tags") else [],
        client_context=g("client_context"),
        seeds=seeds, call=call, repeat=repeat, resp=resp,
        db_host=g("db.host"), db_database=g("db.database"), db_checks=db_checks,
        kafka_bootstrap=g("kafka.bootstrap"), kafka_checks=kafka_checks,
        calls=calls, call_steps=_parse_extra_calls(row, g, _ctx),
        db_delay_ms=int(g("db.delay_ms")) if g("db.delay_ms") else 0,
        notes=g("notes"), id_key=g("id_key") or "flow_id", raw=row,
    )


def is_new_format(fieldnames: list[str]) -> bool:
    """Detect new flat-seed format (seed.path) vs old numbered format (seed1.path)."""
    return "seed.path" in fieldnames


def parse_case_new(rows: list[dict], interpolate: bool = True) -> Case:
    """Parse a case from one or more rows using the new single seed.* column format.

    The first row carries all metadata (call, repeat, resp, db, kafka, calls).
    Every row (including the first) contributes one seed via the seed.* columns.

    ``interpolate=False`` keeps ``{{uuid[:name]}}`` templates intact — used when
    importing a CSV into an editable DB TestCase so fresh UUIDs are generated per
    run (via case_from_dict) rather than frozen at import time.
    """
    first = rows[0]
    _ctx: dict = {}
    _i = (lambda s: _interpolate(s, _ctx)) if interpolate else (lambda s: s)
    g = lambda k: _i((first.get(k) or "").strip())

    # Rows sharing (method, path, scenario) define a multi-response SEQUENCE,
    # served in row order (matches the mock's seq_cursor model). Order of first
    # appearance is preserved.
    seeds: list[SeedGroup] = []
    by_key: dict[tuple, SeedGroup] = {}
    for i, row in enumerate(rows):
        r = lambda k, _row=row: _i((_row.get(k) or "").strip())
        if not r("seed.path"):
            continue
        match_cell = r("seed.match")
        mk, mv = "", ""
        if match_cell:
            mm = parse_map(match_cell)
            if mm:
                mk, mv = next(iter(mm.items()))
        method = r("seed.method") or "POST"
        key = (method, r("seed.path"), r("seed.scenario"))
        resp = _resp_from_cell(r("seed.resp")) if r("seed.resp") else None
        seq = r("seed.is_sequence").lower() in ("1", "true", "yes")
        if key in by_key:
            grp = by_key[key]
            if resp is not None:
                grp.responses.append(resp)
            grp.is_sequence = True  # >1 row for same scenario => sequence
            continue
        grp = SeedGroup(
            index=len(seeds) + 1,
            path=r("seed.path"), method=method,
            scenario=r("seed.scenario"),
            priority=int(r("seed.priority") or 0),
            match_key=mk, match_value=mv,
            is_sequence=seq,
            run_id=r("seed.run_id") or "",
            responses=[resp] if resp is not None else [],
        )
        by_key[key] = grp
        seeds.append(grp)

    body = {k[len("call.body."):]: _coerce(_i(v.strip()))
            for k, v in first.items() if k.startswith("call.body.") and (v or "").strip()}
    call = {
        "method": g("call.method") or "POST",
        "url": g("call.url"),
        "headers": parse_map(g("call.headers")) if g("call.headers") else {},
        "body": body,
        "expect_status": int(g("call.expect_status")) if g("call.expect_status") else None,
    }
    repeat = {
        "same_flow_id": int(g("repeat.same_flow_id") or 1),
        "distinct_ids": int(g("repeat.distinct_ids") or 1),
        "concurrent": int(g("repeat.concurrent") or 1),
    }
    resp = {
        "status": int(g("resp.status")) if g("resp.status") else None,
        "body": parse_map(g("resp.body")) if g("resp.body") else {},
    }

    db_checks = _parse_db_checks(g)

    kafka_checks: list[KafkaCheck] = []
    k = 1
    while g(f"kafka{k}.topic"):
        exp_cell = g(f"kafka{k}.expect")
        expect: dict | str = "absent" if exp_cell == "absent" else parse_map(exp_cell)
        kafka_checks.append(KafkaCheck(topic=g(f"kafka{k}.topic"), key=g(f"kafka{k}.key"), expect=expect))
        k += 1

    calls = parse_calls_cell(g("calls")) if g("calls") else {}

    return Case(
        case_id=g("case_id"), flow_id=g("flow_id") or g("case_id"),
        tags=split_escaped(g("tags")) if g("tags") else [],
        client_context=g("client_context"),
        seeds=seeds, call=call, repeat=repeat, resp=resp,
        db_host=g("db.host"), db_database=g("db.database"), db_checks=db_checks,
        kafka_bootstrap=g("kafka.bootstrap"), kafka_checks=kafka_checks,
        calls=calls, call_steps=_parse_extra_calls(first, g, _ctx, interp=interpolate),
        db_delay_ms=int(g("db.delay_ms")) if g("db.delay_ms") else 0,
        notes=g("notes"), id_key=g("id_key") or "flow_id", raw=first,
    )


# --- structured (dict) <-> Case conversion for DB-stored, editable cases ----
def case_to_dict(case: Case) -> dict:
    """Serialise a parsed Case to the JSON definition stored on TestCase.definition."""
    return {
        "case_id": case.case_id,
        "flow_id": case.flow_id,
        "id_key": case.id_key,
        "tags": case.tags,
        "notes": case.notes,
        "client_context": case.client_context,
        "seeds": [{
            "method": s.method, "path": s.path, "scenario": s.scenario,
            "priority": s.priority, "match_key": s.match_key, "match_value": s.match_value,
            "is_sequence": s.is_sequence, "responses": s.responses, "run_id": s.run_id,
        } for s in case.seeds],
        "call": case.call,
        "call_steps": case.call_steps,
        "repeat": case.repeat,
        "resp": case.resp,
        "db": {"host": case.db_host, "database": case.db_database, "delay_ms": case.db_delay_ms},
        "db_checks": [{"table": d.table, "where": d.where, "expect": d.expect} for d in case.db_checks],
        "calls": case.calls,
        "kafka": {"bootstrap": case.kafka_bootstrap},
        "kafka_checks": [{"topic": k.topic, "key": k.key, "expect": k.expect} for k in case.kafka_checks],
    }


def case_from_dict(definition: dict, interpolate: bool = True) -> Case:
    """Build a runnable Case from a stored JSON definition.

    ``{{uuid[:name]}}`` templates in every string are resolved against ONE shared
    context, so a value referenced in the call body (e.g. flow-{{uuid:flow}}) and
    in a db_check where-clause resolve to the same UUID within the run.
    """
    _ctx: dict = {}

    def deep(v):
        if isinstance(v, str):
            return _interpolate(v, _ctx) if interpolate else v
        if isinstance(v, dict):
            return {k: deep(x) for k, x in v.items()}
        if isinstance(v, list):
            return [deep(x) for x in v]
        return v

    seeds: list[SeedGroup] = []
    for i, s in enumerate(definition.get("seeds", []) or [], 1):
        responses = []
        for r in (s.get("responses") or []):
            rr = dict(r)
            if rr.get("raw"):
                rr["raw"] = deep(rr["raw"])
            if rr.get("canonical") is not None:
                rr["canonical"] = deep(rr["canonical"])
            responses.append(rr)
        seeds.append(SeedGroup(
            index=i, path=deep(s.get("path", "")), method=s.get("method", "POST"),
            scenario=deep(s.get("scenario", "")), priority=int(s.get("priority", 0) or 0),
            match_key=s.get("match_key", "") or "", match_value=deep(s.get("match_value", "") or ""),
            is_sequence=bool(s.get("is_sequence")) or len(responses) > 1,
            run_id=deep(s.get("run_id", "") or ""),
            responses=responses,
        ))

    call = deep(definition.get("call", {}) or {})
    call.setdefault("method", "POST")
    call.setdefault("headers", {})
    call.setdefault("body", {})
    call.setdefault("expect_status", None)

    repeat = definition.get("repeat", {}) or {}
    repeat = {"same_flow_id": int(repeat.get("same_flow_id", 1) or 1),
              "distinct_ids": int(repeat.get("distinct_ids", 1) or 1),
              "concurrent": int(repeat.get("concurrent", 1) or 1)}

    resp = definition.get("resp", {}) or {}
    resp = {"status": resp.get("status"), "body": deep(resp.get("body", {}) or {})}

    db = definition.get("db", {}) or {}
    db_checks = [DbCheck(table=c.get("table", ""), where=deep(c.get("where", {}) or {}),
                         expect=deep(c.get("expect", {}) or {}))
                 for c in (definition.get("db_checks") or [])]
    kafka = definition.get("kafka", {}) or {}
    kafka_checks = [KafkaCheck(topic=c.get("topic", ""), key=deep(c.get("key", "")),
                               expect=deep(c.get("expect", {})))
                    for c in (definition.get("kafka_checks") or [])]

    return Case(
        case_id=definition.get("case_id", ""),
        flow_id=deep(definition.get("flow_id", "")) or definition.get("case_id", ""),
        tags=definition.get("tags", []) or [],
        client_context=definition.get("client_context", ""),
        seeds=seeds, call=call, repeat=repeat, resp=resp,
        db_host=db.get("host", ""), db_database=db.get("database", ""), db_checks=db_checks,
        kafka_bootstrap=kafka.get("bootstrap", ""), kafka_checks=kafka_checks,
        calls={deep(k): v for k, v in (definition.get("calls", {}) or {}).items()},
        call_steps=[deep(s) for s in (definition.get("call_steps") or [])],
        db_delay_ms=int(db.get("delay_ms", 0) or 0),
        notes=definition.get("notes", ""),
        id_key=definition.get("id_key", "flow_id") or "flow_id",
        raw=definition,
    )


def validate(case: Case) -> list[str]:
    """Return a list of MUST-rule violations (§12.3). Empty = valid."""
    errs: list[str] = []
    if not case.case_id:
        errs.append("case_id is required")
    if not case.call["url"]:
        errs.append("call.url is required")
    # Seeds are optional: a case may just drive the AUT and validate the
    # response (or just fire a call). Seed groups that ARE present are still
    # validated below.

    names = [s.scenario for s in case.seeds]
    if len(names) != len(set(names)):
        errs.append("seedN.scenario names must be unique within the case")

    for s in case.seeds:
        if not s.path or not s.scenario or not s.resp:
            errs.append(f"seed{s.index}: path, scenario and resp are all required")
        if s.resp and "status" not in s.resp:
            errs.append(f"seed{s.index}.resp: status is required")
        if s.resp.get("raw") and s.resp.get("canonical"):
            errs.append(f"seed{s.index}.resp: provide raw OR body fields, not both")

    # endpoints shared by >1 seed group must be disambiguated
    by_path: dict[str, list[SeedGroup]] = {}
    for s in case.seeds:
        by_path.setdefault((s.method, s.path), []).append(s)
    for (_, path), grp in by_path.items():
        if len(grp) > 1 and not all(s.match_key or s.priority for s in grp):
            errs.append(f"endpoint {path}: multiple seed groups need match or priority")

    # db.host may be omitted when DB_HOST env var is set (runner falls back to it)
    if case.kafka_checks and not case.kafka_bootstrap:
        errs.append("kafka.bootstrap is required when any kafkaN.* is present")
    return errs
