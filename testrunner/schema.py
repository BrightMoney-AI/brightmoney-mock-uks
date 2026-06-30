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
    flow_id: str
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
    notes: str = ""
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
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _parse_extra_calls(first: dict, g, _ctx: dict) -> list:
    """Parse call2.*, call3.*, … into sequential call steps."""
    steps = []
    for n in range(2, 10):
        if not g(f"call{n}.url"):
            break
        body = {k[len(f"call{n}.body."):]: _coerce(_interpolate(v.strip(), _ctx))
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

    # dbN.* checks
    db_checks: list[DbCheck] = []
    j = 1
    while g(f"db{j}.table"):
        db_checks.append(DbCheck(
            table=g(f"db{j}.table"),
            where=parse_map(g(f"db{j}.where")) if g(f"db{j}.where") else {},
            expect=parse_map(g(f"db{j}.expect")) if g(f"db{j}.expect") else {},
        ))
        j += 1

    # kafkaN.* checks
    kafka_checks: list[KafkaCheck] = []
    k = 1
    while g(f"kafka{k}.topic"):
        exp_cell = g(f"kafka{k}.expect")
        expect: dict | str = "absent" if exp_cell == "absent" else parse_map(exp_cell)
        kafka_checks.append(KafkaCheck(topic=g(f"kafka{k}.topic"), key=g(f"kafka{k}.key"), expect=expect))
        k += 1

    calls = {}
    if g("calls"):
        calls = {kk: int(vv) for kk, vv in parse_map(g("calls")).items()}

    return Case(
        case_id=g("case_id"), flow_id=g("flow_id") or g("case_id"),
        tags=split_escaped(g("tags")) if g("tags") else [],
        client_context=g("client_context"),
        seeds=seeds, call=call, repeat=repeat, resp=resp,
        db_host=g("db.host"), db_database=g("db.database"), db_checks=db_checks,
        kafka_bootstrap=g("kafka.bootstrap"), kafka_checks=kafka_checks,
        calls=calls, call_steps=_parse_extra_calls(row, g, _ctx),
        notes=g("notes"), raw=row,
    )


def is_new_format(fieldnames: list[str]) -> bool:
    """Detect new flat-seed format (seed.path) vs old numbered format (seed1.path)."""
    return "seed.path" in fieldnames


def parse_case_new(rows: list[dict]) -> Case:
    """Parse a case from one or more rows using the new single seed.* column format.

    The first row carries all metadata (call, repeat, resp, db, kafka, calls).
    Every row (including the first) contributes one seed via the seed.* columns.
    """
    first = rows[0]
    _ctx: dict = {}
    g = lambda k: _interpolate((first.get(k) or "").strip(), _ctx)

    # Rows sharing (method, path, scenario) define a multi-response SEQUENCE,
    # served in row order (matches the mock's seq_cursor model). Order of first
    # appearance is preserved.
    seeds: list[SeedGroup] = []
    by_key: dict[tuple, SeedGroup] = {}
    for i, row in enumerate(rows):
        r = lambda k, _row=row: _interpolate((_row.get(k) or "").strip(), _ctx)
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
            responses=[resp] if resp is not None else [],
        )
        by_key[key] = grp
        seeds.append(grp)

    body = {k[len("call.body."):]: _coerce(_interpolate(v.strip(), _ctx))
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

    db_checks: list[DbCheck] = []
    j = 1
    while g(f"db{j}.table"):
        db_checks.append(DbCheck(
            table=g(f"db{j}.table"),
            where=parse_map(g(f"db{j}.where")) if g(f"db{j}.where") else {},
            expect=parse_map(g(f"db{j}.expect")) if g(f"db{j}.expect") else {},
        ))
        j += 1

    kafka_checks: list[KafkaCheck] = []
    k = 1
    while g(f"kafka{k}.topic"):
        exp_cell = g(f"kafka{k}.expect")
        expect: dict | str = "absent" if exp_cell == "absent" else parse_map(exp_cell)
        kafka_checks.append(KafkaCheck(topic=g(f"kafka{k}.topic"), key=g(f"kafka{k}.key"), expect=expect))
        k += 1

    calls = {}
    if g("calls"):
        calls = {kk: int(vv) for kk, vv in parse_map(g("calls")).items()}

    return Case(
        case_id=g("case_id"), flow_id=g("flow_id") or g("case_id"),
        tags=split_escaped(g("tags")) if g("tags") else [],
        client_context=g("client_context"),
        seeds=seeds, call=call, repeat=repeat, resp=resp,
        db_host=g("db.host"), db_database=g("db.database"), db_checks=db_checks,
        kafka_bootstrap=g("kafka.bootstrap"), kafka_checks=kafka_checks,
        calls=calls, call_steps=_parse_extra_calls(first, g, _ctx),
        notes=g("notes"), raw=first,
    )


def validate(case: Case) -> list[str]:
    """Return a list of MUST-rule violations (§12.3). Empty = valid."""
    errs: list[str] = []
    if not case.case_id:
        errs.append("case_id is required")
    if not case.call["url"]:
        errs.append("call.url is required")
    if not case.seeds:
        errs.append("at least one seed group (seed1.*) is required")

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

    if case.db_checks and not case.db_host:
        errs.append("db.host is required when any dbN.* is present")
    if case.kafka_checks and not case.kafka_bootstrap:
        errs.append("kafka.bootstrap is required when any kafkaN.* is present")
    return errs
