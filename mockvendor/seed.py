"""Seeding helpers — turn scenario definitions into ORM rows (design §5, §8).

Two entry points:

* ``seed_scenario_dict(payload)`` — used by the DRF admin API and the test
  runner; ``payload`` is one endpoint+scenario+response(s) group.
* ``seed_scenarios_csv(path)`` — used by the ``seed_scenarios`` management
  command; a "scenario library" CSV, one Response row per line.

Both converge on ``_upsert`` so the rules stay in one place.
"""
from __future__ import annotations

import csv
import json

from django.db import transaction

from .models import CallLog, Endpoint, Format, Response, Scenario, ScenarioBundle

DEFAULT_FORMATS = {
    "json": ("application/json", "mockvendor.serializers_fmt.JsonSerializer"),
    "xml": ("application/xml", "mockvendor.serializers_fmt.XmlSerializer"),
}


def ensure_format(name: str) -> Format:
    name = (name or "json").strip()
    fmt = Format.objects.filter(name=name).first()
    if fmt:
        return fmt
    content_type, path = DEFAULT_FORMATS.get(name, ("application/octet-stream", ""))
    return Format.objects.create(name=name, content_type=content_type, serializer_path=path)


@transaction.atomic
def _upsert(method: str, path: str, default_format: str, scenario_name: str,
            priority: int, is_sequence: bool, match_key: str, match_value: str,
            responses: list[dict], run_id: str = "") -> Scenario:
    fmt_default = ensure_format(default_format)
    endpoint, _ = Endpoint.objects.get_or_create(
        method=method.upper(), path_pattern=path,
        defaults={"default_format": fmt_default, "enabled": True},
    )
    # Replace any same-named scenario for this endpoint+run (idempotent seeding).
    Scenario.objects.filter(endpoint=endpoint, name=scenario_name, run_id=run_id).delete()
    scenario = Scenario.objects.create(
        endpoint=endpoint, name=scenario_name, priority=priority,
        is_sequence=is_sequence or len(responses) > 1,
        match_key=match_key or "", match_value=match_value or "", run_id=run_id,
    )
    for i, r in enumerate(responses):
        Response.objects.create(
            scenario=scenario,
            seq_index=(i if (is_sequence or len(responses) > 1) else None),
            status_code=int(r.get("status", 200)),
            format=ensure_format(r.get("format") or default_format),
            canonical=r.get("canonical"),
            raw_override=r.get("raw", "") or "",
            headers=r.get("headers") or {},
            delay_ms=int(r.get("delay_ms", 0) or 0),
        )
    return scenario


def seed_scenario_dict(payload: dict, run_id: str = "") -> Scenario:
    """payload: {method, path, default_format, scenario, priority, is_sequence,
    match_key, match_value, responses:[{status,format,canonical,raw,headers,delay_ms}]}."""
    return _upsert(
        method=payload.get("method", "POST"),
        path=payload["path"],
        default_format=payload.get("default_format", "json"),
        scenario_name=payload["scenario"],
        priority=int(payload.get("priority", 0)),
        is_sequence=bool(payload.get("is_sequence", False)),
        match_key=payload.get("match_key", ""),
        match_value=payload.get("match_value", ""),
        responses=payload.get("responses", []),
        run_id=run_id or payload.get("run_id", ""),
    )


def reset(run_id: str = "") -> dict:
    """Clear scenarios AND CallLog for full isolation (design §8 /mock/admin/reset)."""
    sc = Scenario.objects.all()
    cl = CallLog.objects.all()
    if run_id:
        sc = sc.filter(run_id=run_id)
    n_sc = sc.count()
    n_cl = cl.count()
    sc.delete()
    cl.delete()
    return {"scenarios_deleted": n_sc, "calllog_deleted": n_cl}


def reset_scenarios(run_id: str = "", all_runs: bool = False) -> dict:
    """Clear seeded Scenarios, leaving CallLog intact.

    Scope (parallel-run aware, see matcher.select):
      * ``all_runs=True``  — every scenario in every namespace (explicit full wipe).
      * ``run_id="<x>"``   — only that namespace (one host / one run).
      * ``run_id=""``      — only the DEFAULT namespace. Wiping the shared default
        set must not disturb another host's parallel per-IP scenarios, so this is
        scoped rather than global.
    """
    sc = Scenario.objects.all()
    if not all_runs:
        sc = sc.filter(run_id=run_id)
    n_sc = sc.count()
    sc.delete()
    return {"scenarios_deleted": n_sc}


# --- scenario bundles: register a group once, replay it by id --------------
def register_bundle(bundle_id: str, definitions: list[dict], run_id: str = "") -> ScenarioBundle:
    """Persist ``definitions`` (a list of seed_scenario_dict-shaped payloads)
    under ``bundle_id`` and seed them immediately. Re-registering the same
    bundle_id overwrites the stored definition (idempotent, like ``_upsert``)."""
    bundle, _ = ScenarioBundle.objects.update_or_create(
        bundle_id=bundle_id, defaults={"definition": definitions, "run_id": run_id},
    )
    for payload in definitions:
        seed_scenario_dict(payload, run_id=run_id or payload.get("run_id", ""))
    return bundle


def implement_bundle(bundle_id: str, run_id: str = "") -> dict:
    """Clear active scenarios (never CallLog) then replay a registered bundle."""
    try:
        bundle = ScenarioBundle.objects.get(bundle_id=bundle_id)
    except ScenarioBundle.DoesNotExist:
        raise ValueError(f"no scenario bundle registered under id={bundle_id!r}")
    effective_run_id = run_id or bundle.run_id
    reset_scenarios(run_id=effective_run_id)
    created = [seed_scenario_dict(p, run_id=effective_run_id or p.get("run_id", ""))
               for p in bundle.definition]
    return {"id": bundle_id, "scenarios_seeded": len(created)}


# --- "scenario library" CSV for the management command ----------------------
# Columns (header): method,path,default_format,scenario,priority,is_sequence,
#                   match_key,match_value,seq_index,status,format,delay_ms,
#                   canonical,raw_override,headers
# One row per Response; rows sharing (path,scenario) are grouped into a sequence.
def seed_scenarios_csv(path: str, run_id: str = "") -> dict:
    groups: dict[tuple, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (row.get("path") and row.get("scenario")):
                continue
            key = (row["path"].strip(), row["scenario"].strip())
            g = groups.setdefault(key, {
                "method": (row.get("method") or "POST").strip(),
                "path": row["path"].strip(),
                "default_format": (row.get("default_format") or "json").strip(),
                "scenario": row["scenario"].strip(),
                "priority": int(row.get("priority") or 0),
                "is_sequence": (row.get("is_sequence") or "").strip().lower() in ("1", "true", "yes"),
                "match_key": (row.get("match_key") or "").strip(),
                "match_value": (row.get("match_value") or "").strip(),
                "responses": [],
            })
            g["responses"].append({
                "status": int(row.get("status") or 200),
                "format": (row.get("format") or "").strip() or None,
                "canonical": _maybe_json(row.get("canonical")),
                "raw": (row.get("raw_override") or "").strip(),
                "headers": _maybe_json(row.get("headers")) or {},
                "delay_ms": int(row.get("delay_ms") or 0),
            })
    created = [seed_scenario_dict(g, run_id=run_id) for g in groups.values()]
    return {"endpoints": len({g["path"] for g in groups.values()}),
            "scenarios": len(created)}


def _maybe_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
