"""Dummy AUT HTTP surface — POST /aut/enroll.

Receives the enrollment body, runs the flow (which calls the mock vendors),
persists an Enrollment row in the AUT's OWN database, and returns the decision.
``flow_id`` is the idempotency key: a repeat call returns the existing result.
"""
from __future__ import annotations

import json

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import flow
from .models import Enrollment


@csrf_exempt
def enroll(request):
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "bad_json"}, status=400)

    flow_id = str(body.get("flow_id") or "").strip()
    if not flow_id:
        return JsonResponse({"error": "flow_id_required"}, status=400)
    bright_uid = str(body.get("bright_uid") or "")
    test_path = str(body.get("test_path") or "")

    # Idempotency: flow_id is UNIQUE; a replay returns the first result.
    existing = Enrollment.objects.filter(flow_id=flow_id).first()
    if existing:
        return JsonResponse(_payload(existing), status=200)

    result = flow.run(flow_id, bright_uid, test_path, body)

    with transaction.atomic(using="aut"):
        obj, _ = Enrollment.objects.get_or_create(
            flow_id=flow_id,
            defaults={
                "bright_uid": bright_uid, "test_path": test_path,
                "decision": result["decision"],
                "persona_inquiry_id": result["persona_inquiry_id"],
                "escalation_type": result["escalation_type"],
                "escalation_status": result["escalation_status"],
            },
        )
    return JsonResponse(_payload(obj), status=200)


def _payload(obj: Enrollment) -> dict:
    return {
        "flow_id": obj.flow_id, "decision": obj.decision,
        "persona_inquiry_id": obj.persona_inquiry_id,
        "escalation_type": obj.escalation_type,
        "escalation_status": obj.escalation_status,
        "error": None,
    }
