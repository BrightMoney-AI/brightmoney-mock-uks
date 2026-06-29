"""A tiny KYC flow-graph engine for the dummy AUT.

It mirrors the straight-through edges of the enrollment diagram:

    IDology  PASS                      -> KYC Pass (PASSED)
             FAIL (escalate=false)     -> LexisNexis
             FAIL (escalate=true)      -> PII escalation: record escalation_type,
                                          client re-enters corrected PII, re-run
                                          IDology once. PASS -> KYC Pass; a
                                          generic re-fail falls through to LexisNexis.
    LexisNexis PASS                    -> KYC Pass
               FAIL_HARD_BLOCK         -> Decline (FAILED)
               FAIL_SSN / FAIL_NON_SSN -> Persona
    Persona  (PENDING)                 -> poll again (eventual consistency)
             PASS + LN non-SSN fail    -> KYC Pass
             PASS + LN SSN fail        -> SSN escalation: same SSN -> Decline;
                                          different SSN -> re-run IDology -> Pass
             FAIL                      -> Decline

The vendor calls go to the mock server over HTTP, exactly as a real client would
(design: "the AUT calls the Mock Server directly"). The mock returns the seeded
body that steers the next edge. Sequence scenarios let a single endpoint return
different bodies across calls (re-entry, polling).
"""
from __future__ import annotations

import json

import requests
from django.conf import settings

# fail_category token -> escalation suffix. Multiple categories combine in order,
# e.g. FAIL_SSN;FAIL_DOB -> ESCALATE_SSN_DOB.
_CATEGORY_SHORT = {
    "FAIL_SSN": "SSN",
    "FAIL_ADDRESS": "ADDRESS",
    "FAIL_NAME": "FULL_NAME",
    "FAIL_DOB": "DOB",
}
_PERSONA_MAX_POLLS = 3


def _mock_base() -> str:
    return getattr(settings, "MOCK_BASE_URL", "http://127.0.0.1:8000")


def _call(path: str, body: dict) -> dict:
    url = _mock_base().rstrip("/") + path
    try:
        resp = requests.post(url, json=body, timeout=getattr(settings, "AUT_VENDOR_TIMEOUT", 10))
    except requests.exceptions.Timeout:
        return {"_status": 504, "_raw": "timeout"}
    except requests.exceptions.RequestException as exc:
        return {"_status": 503, "_raw": str(exc)}
    try:
        return {"_status": resp.status_code, **(resp.json() if resp.content else {})}
    except (ValueError, json.JSONDecodeError):
        return {"_status": resp.status_code, "_raw": resp.text}


def _is_error(r: dict) -> bool:
    """5xx, transport failure / unparseable body, or auth failure -> terminal ERROR."""
    return r.get("_status", 200) >= 500 or "_raw" in r or r.get("_status") in (401, 403)


def _escalation_type(fail_category: str) -> str:
    cats = [c.strip() for c in str(fail_category).split(";") if c.strip()]
    parts = [_CATEGORY_SHORT[c] for c in cats if c in _CATEGORY_SHORT]
    return "ESCALATE_" + "_".join(parts) if parts else "ESCALATE_PII"


def _idology(body: dict) -> dict:
    return _call("/vendor/idology/verify", body)


def _poll_persona(body: dict) -> dict:
    """Call Persona, polling while the inquiry is still PENDING (eventual consistency).

    A non-sequence Persona scenario returns a terminal body on the first call, so
    the loop breaks immediately (one call). The eventual-consistency scenario
    returns PENDING, PENDING, COMPLETED across three calls.
    """
    pe: dict = {}
    for _ in range(_PERSONA_MAX_POLLS):
        pe = _call("/vendor/persona/inquiry", body)
        if _is_error(pe):
            return pe
        state = str(pe.get("state", "")).upper()
        if "persona_kyc_verified" in pe or state == "COMPLETED":
            break
        # PENDING -> poll again
    return pe


def run(flow_id: str, bright_uid: str, test_path: str, body: dict) -> dict:
    """Return a result dict the view persists + echoes."""
    result = {
        "flow_id": flow_id, "bright_uid": bright_uid, "test_path": test_path,
        "decision": "ERROR", "persona_inquiry_id": None,
        "escalation_type": "", "escalation_status": "",
    }

    # ---- IDology (call 1) ----
    idr = _idology(body)
    if _is_error(idr):
        return result  # terminal ERROR
    sr = str(idr.get("summary_result", "")).upper()
    if sr == "PASS":
        result["decision"] = "PASSED"
        return result

    # IDology FAIL + escalate -> PII escalation; client re-enters corrected PII
    # and IDology is re-run once.
    if str(idr.get("escalate", "")).lower() == "true":
        result["escalation_type"] = _escalation_type(idr.get("fail_category", ""))
        result["escalation_status"] = "DIFFERENT_INPUT"
        idr = _idology(body)  # re-entry (call 2)
        if _is_error(idr):
            return result
        sr = str(idr.get("summary_result", "")).upper()
        if sr == "PASS":
            result["decision"] = "PASSED"
            return result
        # else: 2nd IDology generic-fails -> fall through to LexisNexis (TC-013)

    # ---- LexisNexis ----
    ln = _call("/vendor/lexisnexis/verify", body)
    if _is_error(ln):
        return result
    res = str(ln.get("result", "")).upper()
    if res == "PASS":
        result["decision"] = "PASSED"
        return result
    if res == "FAIL_HARD_BLOCK":
        result["decision"] = "FAILED"
        return result
    if res not in ("FAIL_SSN", "FAIL_NON_SSN"):
        return result  # unknown LN body -> ERROR
    ln_ssn_fail = res == "FAIL_SSN"

    # ---- Persona (with eventual-consistency polling) ----
    pe = _poll_persona(body)
    if _is_error(pe):
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
    reenter = str(body.get("reenter", "different")).lower()
    if reenter == "same":
        # Same SSN re-entered -> Decline. No IDology re-run.
        result["escalation_status"] = "SAME_INPUT"
        result["decision"] = "FAILED"
        return result

    # Different SSN re-entered -> re-run IDology to resolve.
    result["escalation_status"] = "DIFFERENT_INPUT"
    idr2 = _idology(body)
    if _is_error(idr2):
        return result
    if str(idr2.get("summary_result", "")).upper() == "PASS":
        result["decision"] = "PASSED"
    else:
        result["decision"] = "FAILED"
    return result
