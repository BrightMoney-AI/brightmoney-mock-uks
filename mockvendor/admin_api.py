"""DRF admin & seed API (design §8).

  POST   /mock/admin/scenarios       create / seed a scenario
  GET    /mock/admin/scenarios       list active scenarios
  PUT    /mock/admin/scenarios/{id}  update (enable/disable, priority)
  DELETE /mock/admin/scenarios/{id}  remove a scenario
  POST   /mock/admin/reset           clear scenarios + CallLog (isolation)
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
from .models import CallLog, Scenario
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
         "delay_applied_ms": c.delay_applied_ms, "created_at": c.created_at.isoformat()}
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
