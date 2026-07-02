"""DRF admin & seed API (design §8).

  POST   /mock/admin/scenarios       create / seed a scenario
  GET    /mock/admin/scenarios       list active scenarios
  PUT    /mock/admin/scenarios/{id}  update (enable/disable, priority)
  DELETE /mock/admin/scenarios/{id}  remove a scenario
  POST   /mock/admin/reset           clear scenarios + CallLog (isolation)
  POST   /mock/admin/reset/scenarios clear scenarios only; CallLog preserved
  POST   /mock/admin/register        save + seed a named group of scenarios ({"id","scenarios":[...]})
  GET    /mock/admin/register        list registered scenario bundles
  DELETE /mock/admin/register/{id}   remove a registered bundle
  POST   /mock/admin/implement       reset scenarios (never CallLog), replay a bundle by {"id"}
  GET    /mock/admin/calls           query CallLog for assertions
  GET    /mock/admin/formats         list registered serializers

Gated behind ``settings.MOCKVENDOR_ADMIN_ENABLED`` so it is unreachable in
production (design §8.1).
"""
from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response as DrfResponse

from . import seed as seed_mod
from .models import CallLog, Scenario, ScenarioBundle
from .serializers_fmt import registry


def _enabled() -> bool:
    return bool(getattr(settings, "MOCKVENDOR_ADMIN_ENABLED", False))


def _forbidden():
    return DrfResponse({"error": "mock admin API disabled"}, status=status.HTTP_403_FORBIDDEN)


def _scenario_json(sc: Scenario) -> dict:
    return {
        "id": sc.id, "endpoint": str(sc.endpoint), "name": sc.name,
        "priority": sc.priority, "is_sequence": sc.is_sequence,
        "enabled": sc.enabled, "match_key": sc.match_key,
        "match_value": sc.match_value, "run_id": sc.run_id,
        "responses": [
            {"seq_index": r.seq_index, "status": r.status_code,
             "format": r.format.name, "delay_ms": r.delay_ms,
             "canonical": r.canonical, "raw_override": r.raw_override}
            for r in sc.responses.all()
        ],
    }


@api_view(["GET", "POST"])
def scenarios(request):
    if not _enabled():
        return _forbidden()
    if request.method == "POST":
        run_id = request.data.get("run_id", "")
        payloads = request.data if isinstance(request.data, list) else [request.data]
        created = [seed_mod.seed_scenario_dict(p, run_id=run_id or p.get("run_id", "")) for p in payloads]
        return DrfResponse({"created": [_scenario_json(s) for s in created]},
                           status=status.HTTP_201_CREATED)
    run_id = request.query_params.get("run_id")
    qs = Scenario.objects.all()
    if run_id is not None:
        qs = qs.filter(run_id=run_id)
    return DrfResponse({"scenarios": [_scenario_json(s) for s in qs]})


@api_view(["PUT", "DELETE"])
def scenario_detail(request, scenario_id: int):
    if not _enabled():
        return _forbidden()
    try:
        sc = Scenario.objects.get(pk=scenario_id)
    except Scenario.DoesNotExist:
        return DrfResponse({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        sc.delete()
        return DrfResponse(status=status.HTTP_204_NO_CONTENT)
    for field in ("priority", "enabled", "match_key", "match_value", "is_sequence"):
        if field in request.data:
            setattr(sc, field, request.data[field])
    sc.save()
    return DrfResponse(_scenario_json(sc))


@api_view(["POST"])
def reset(request):
    if not _enabled():
        return _forbidden()
    run_id = request.data.get("run_id", "") if isinstance(request.data, dict) else ""
    return DrfResponse(seed_mod.reset(run_id=run_id))


@api_view(["POST"])
def reset_scenarios(request):
    """Clear only seeded Scenarios; CallLog is preserved for post-run inspection."""
    if not _enabled():
        return _forbidden()
    run_id = request.data.get("run_id", "") if isinstance(request.data, dict) else ""
    return DrfResponse(seed_mod.reset_scenarios(run_id=run_id))


def _bundle_json(b: ScenarioBundle) -> dict:
    return {"id": b.bundle_id, "scenarios": len(b.definition), "run_id": b.run_id,
            "modified_at": b.modified_at.isoformat()}


@api_view(["GET", "POST"])
def register(request):
    """POST: save a named group of scenario definitions and seed them immediately.

    Body: {"id": "<bundle_id>", "scenarios": [<seed_scenario_dict payload>, ...], "run_id"?: str}
    Re-registering the same id overwrites the stored definition.
    """
    if not _enabled():
        return _forbidden()
    if request.method == "GET":
        return DrfResponse({"bundles": [_bundle_json(b) for b in ScenarioBundle.objects.all()]})
    bundle_id = request.data.get("id")
    definitions = request.data.get("scenarios")
    if not bundle_id or not isinstance(definitions, list) or not definitions:
        return DrfResponse({"error": "body must be {'id': str, 'scenarios': [...]}"},
                           status=status.HTTP_400_BAD_REQUEST)
    run_id = request.data.get("run_id", "")
    bundle = seed_mod.register_bundle(bundle_id, definitions, run_id=run_id)
    return DrfResponse({"id": bundle.bundle_id, "scenarios_registered": len(definitions)},
                       status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def register_detail(request, bundle_id: str):
    if not _enabled():
        return _forbidden()
    deleted, _ = ScenarioBundle.objects.filter(bundle_id=bundle_id).delete()
    if not deleted:
        return DrfResponse({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    return DrfResponse(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def implement(request):
    """Clear active scenarios (CallLog is NEVER touched) then replay a registered
    bundle by id. Body: {"id": "<bundle_id>", "run_id"?: str}."""
    if not _enabled():
        return _forbidden()
    bundle_id = request.data.get("id")
    if not bundle_id:
        return DrfResponse({"error": "body must be {'id': str}"}, status=status.HTTP_400_BAD_REQUEST)
    run_id = request.data.get("run_id", "")
    try:
        result = seed_mod.implement_bundle(bundle_id, run_id=run_id)
    except ValueError as exc:
        return DrfResponse({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return DrfResponse(result)


@api_view(["GET"])
def calls(request):
    if not _enabled():
        return _forbidden()
    qs = CallLog.objects.all()
    path = request.query_params.get("path")
    if path:
        qs = qs.filter(request_path=path)
    rows = [
        {"id": c.id, "method": c.request_method, "path": c.request_path,
         "status": c.response_status, "scenario": c.scenario.name if c.scenario else None,
         "response_body": c.response_body, "delay_applied_ms": c.delay_applied_ms,
         "created_at": c.created_at.isoformat()}
        for c in qs
    ]
    # Convenience: per-path counts (used by the runner's `calls` assertions).
    counts: dict[str, int] = {}
    for c in qs:
        counts[c.request_path] = counts.get(c.request_path, 0) + 1
    return DrfResponse({"count": qs.count(), "counts": counts, "calls": rows})


@api_view(["GET"])
def formats(request):
    if not _enabled():
        return _forbidden()
    return DrfResponse({"formats": [
        {"name": name, "content_type": s.content_type} for name, s in registry().items()
    ]})
