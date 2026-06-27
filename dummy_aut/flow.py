"""A tiny KYC flow-graph engine for the dummy AUT.

It mirrors the straight-through edges of the enrollment diagram:

    IDology  PASS                      -> KYC Pass (PASSED)
             FAIL (escalate=false)     -> LexisNexis
             FAIL (escalate=true)      -> PII escalation (records escalation_type)
    LexisNexis PASS                    -> KYC Pass
               FAIL_HARD_BLOCK         -> Decline (FAILED)
               FAIL_SSN / FAIL_NON_SSN -> Persona
    Persona  PASS + LN non-SSN fail    -> KYC Pass
             PASS + LN SSN fail        -> SSN escalation
             FAIL                      -> Decline

The vendor calls go to the mock server over HTTP, exactly as a real client would
(design: "the AUT calls the Mock Server directly"). The mock returns the seeded
body that steers the next edge.
"""
from __future__ import annotations

import json

import requests
from django.conf import settings

_CATEGORY_TO_ESCALATION = {
    "FAIL_SSN": "ESCALATE_SSN",
    "FAIL_ADDRESS": "ESCALATE_ADDRESS",
    "FAIL_NAME": "ESCALATE_FULL_NAME",
    "FAIL_DOB": "ESCALATE_DOB",
}


def _mock_base() -> str:
    return getattr(settings, "MOCK_BASE_URL", "http://127.0.0.1:8000")


def _call(path: str, body: dict) -> dict:
    url = _mock_base().rstrip("/") + path
    resp = requests.post(url, json=body, timeout=getattr(settings, "AUT_VENDOR_TIMEOUT", 10))
    try:
        return {"_status": resp.status_code, **(resp.json() if resp.content else {})}
    except (ValueError, json.JSONDecodeError):
        return {"_status": resp.status_code, "_raw": resp.text}


def run(flow_id: str, bright_uid: str, test_path: str, body: dict) -> dict:
    """Return a result dict the view persists + echoes."""
    result = {
        "flow_id": flow_id, "bright_uid": bright_uid, "test_path": test_path,
        "decision": "ERROR", "persona_inquiry_id": None,
        "escalation_type": "", "escalation_status": "",
    }

    # ---- IDology ----
    idr = _call("/vendor/idology/verify", body)
    if idr.get("_status", 200) >= 500 or "_raw" in idr:
        result["decision"] = "ERROR"
        return result
    sr = str(idr.get("summary_result", "")).upper()
    if idr.get("_status") in (401, 403):
        result["decision"] = "ERROR"
        return result
    if sr == "PASS":
        result["decision"] = "PASSED"
        return result
    # IDology FAIL
    if str(idr.get("escalate", "")).lower() == "true":
        cat = str(idr.get("fail_category", "")).split(";")[0]
        result["escalation_type"] = _CATEGORY_TO_ESCALATION.get(cat, "ESCALATE_PII")
        result["escalation_status"] = "DIFFERENT_INPUT"
        # PII re-entry would re-run IDology; for the dummy AUT we stop here in
        # PENDING (the real AUT resumes via its own endpoint).
        result["decision"] = "PENDING"
        return result

    # ---- LexisNexis ----
    ln = _call("/vendor/lexisnexis/verify", body)
    if ln.get("_status", 200) >= 500 or "_raw" in ln:
        result["decision"] = "ERROR"
        return result
    res = str(ln.get("result", "")).upper()
    if res == "PASS":
        result["decision"] = "PASSED"
        return result
    if res == "FAIL_HARD_BLOCK":
        result["decision"] = "FAILED"
        return result
    if res not in ("FAIL_SSN", "FAIL_NON_SSN"):
        result["decision"] = "ERROR"
        return result
    ln_ssn_fail = res == "FAIL_SSN"

    # ---- Persona ----
    pe = _call("/vendor/persona/inquiry", body)
    if pe.get("_status", 200) >= 500 or "_raw" in pe:
        result["decision"] = "ERROR"
        return result
    result["persona_inquiry_id"] = f"pi-{flow_id}"
    verified = str(pe.get("persona_kyc_verified", "")).lower() == "true"
    if not verified:
        result["decision"] = "FAILED"
        return result
    if not ln_ssn_fail:
        result["decision"] = "PASSED"
        return result

    # Persona PASS but LexisNexis SSN-failed -> SSN escalation.
    result["escalation_type"] = "ESCALATE_SSN"
    # The client's re-entry decides: a body hint `reenter=same|different`.
    reenter = str(body.get("reenter", "different")).lower()
    if reenter == "same":
        result["escalation_status"] = "SAME_INPUT"
        result["decision"] = "FAILED"  # same SSN -> Decline
    else:
        result["escalation_status"] = "DIFFERENT_INPUT"
        result["decision"] = "PASSED"  # different SSN -> re-run resolves to pass
    return result
