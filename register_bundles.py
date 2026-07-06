"""Register named scenario bundles and implement one on demand.

Uses the mockvendor admin API (mockvendor/urls.py, mockvendor/admin_api.py):

  POST /mock/admin/register    save + seed a named bundle: {"id", "scenarios":[...]}
  POST /mock/admin/implement   reset scenarios (CallLog untouched), replay bundle: {"id"}
  GET  /mock/admin/scenarios   list active scenarios
  GET  /mock/admin/calls       query CallLog

Each bundle groups every vendor response a given flow-graph case needs
(reference: data/test_suite_full.csv), so "implement"-ing a case id swaps the
whole mock DB over to that case's responses in one call, without touching
CallLog (so assertions against prior calls in the same run still work).

Usage:
    python register_bundles.py register            # register all bundles below
    python register_bundles.py implement <case_id>  # e.g. idology-fail-escalation
    python register_bundles.py list                 # show registered bundles
"""
from __future__ import annotations

import sys

import requests

BASE = "http://127.0.0.1:8000/mock/admin"


def scenario(method, path, scenario_name, responses, default_format="json",
             priority=5, is_sequence=False, match_key="", match_value=""):
    return {
        "method": method,
        "path": path,
        "default_format": default_format,
        "scenario": scenario_name,
        "priority": priority,
        "is_sequence": is_sequence,
        "match_key": match_key,
        "match_value": match_value,
        "responses": responses,
    }


def resp(status=200, format=None, raw="", canonical=None, delay_ms=0, headers=None):
    return {
        "status": status,
        "format": format,
        "raw": raw,
        "canonical": canonical,
        "delay_ms": delay_ms,
        "headers": headers or {},
    }


USM_PROFILE_DEFAULT = scenario(
    "POST", "/api/v1/users/get_user_profile_data/", "usm-profile-default",
    [resp(canonical={
        "bright_uid": "{{uuid:uid}}",
        "primary_email": "john@example.com",
        "primary_phonenum": "+11234567890",
        "first_name": "John",
        "last_name": "Doe",
        "date_of_birth": "1990-05-15",
        "is_kyc_verified": False,
        "zip_code": "78701",
        "address": {
            "address_type": "bright",
            "manual_address": {
                "apt": "", "zip": "78701", "city": "Austin",
                "state": {"long_name": "Texas", "short_name": "TX"},
                "street": "123 Main St",
            },
        },
        "ssn_encrypted": (
            "gAAAAABqQnJ4S7_gd7u2iaMV_8jNjzBVe4mkZcJVYDdxDbjrKQj6cvjhPR9Avyo2VKbuE8efqeBjeZIZugYZY23qE8EoKeh7fg=="
        ),
        "ip": "192.168.1.1",
        "age": 35,
        "acquired_on": "IOS_APP",
        "is_deleted": False,
        "error": {},
    })],
)

LN_TOKEN = scenario(
    "POST", "/LN.WebServices/api/OAuth2/Token", "ln-token",
    [resp(canonical={"access_token": "tok_{{uuid:tok}}", "expires_in": 3600})],
)


# --- data/test_suite_full.csv case IDO-03 --------------------------------
# IDology FAIL_SSN -> LexisNexis(any fail) -> Persona create PENDING ->
# /status_details -> /resume persona_kyc_verified=PASS -> ESCALATE_SSN parks PENDING.
IDOLOGY_FAIL_ESCALATION = "idology-fail-escalation"
IDOLOGY_FAIL_ESCALATION_SCENARIOS = [
    scenario(
        "POST", "/vendor/idology/verify", "idology-fail-ssn",
        [resp(format="xml", raw=(
            '<?xml version="1.0"?><response>'
            '<summary-result><key>id.failure</key><message>FAIL</message></summary-result>'
            '<results><key>result.match</key><message>ID Located</message></results>'
            '<qualifiers><qualifier><key>resultcode.ssn.does.not.match</key></qualifier></qualifiers>'
            '</response>'
        ))],
        default_format="xml",
    ),
    LN_TOKEN,
    scenario(
        "POST", "/LN.WebServices/api/Lists/Search", "ln-ssn",
        [resp(canonical={"Records": [{"InstantIDIndividual": {
            "ComprehensiveVerificationIndex": 10,
            "NameAddressSSN": {"RiskCode": 2},
            "NameAddressPhone": {"RiskCode": 12},
        }}]})],
    ),
    scenario(
        "POST", "/api/v1/inquiries", "persona-create",
        [resp(canonical={"data": {"id": "inq_{{uuid:inq}}", "type": "inquiry",
                                   "attributes": {"status": "created", "reference-id": "{{uuid:uid}}"}}})],
    ),
    USM_PROFILE_DEFAULT,
]

# --- data/test_suite_full.csv case TC-001 --------------------------------
# IDology PASS -> flow PASSED, no escalation.
IDOLOGY_PASS = "idology-pass"
IDOLOGY_PASS_SCENARIOS = [
    scenario(
        "POST", "/vendor/idology/verify", "idology-pass",
        [resp(format="xml", raw=(
            '<?xml version="1.0"?><response>'
            '<summary-result><key>id.success</key><message>PASS</message></summary-result>'
            '<results><key>result.match</key><message>ID Located</message></results>'
            '</response>'
        ))],
        default_format="xml",
    ),
    USM_PROFILE_DEFAULT,
]

# --- data/test_suite_full.csv case LN-06 ---------------------------------
# IDology FAIL -> LexisNexis hard-block -> terminal FAILED, no escalation.
LEXISNEXIS_HARD_BLOCK = "lexisnexis-hard-block"
LEXISNEXIS_HARD_BLOCK_SCENARIOS = [
    scenario(
        "POST", "/vendor/idology/verify", "idology-fail-no-esc",
        [resp(format="xml", raw=(
            '<?xml version="1.0"?><response>'
            '<summary-result><key>id.failure</key><message>FAIL</message></summary-result>'
            '<results><key>result.match</key><message>ID Located</message></results>'
            '<qualifiers><qualifier><key>resultcode.ssn.does.not.match</key></qualifier></qualifiers>'
            '</response>'
        ))],
        default_format="xml",
    ),
    LN_TOKEN,
    scenario(
        "POST", "/LN.WebServices/api/Lists/Search", "ln-hard-it",
        [resp(canonical={"Records": [{"InstantIDIndividual": {
            "ComprehensiveVerificationIndex": 50,
            "NameAddressSSN": {"RiskCode": 12},
            "NameAddressPhone": {"RiskCode": 12},
            "RiskIndicators": [{"RiskCode": "IT"}],
        }}]})],
    ),
    USM_PROFILE_DEFAULT,
]

BUNDLES = {
    IDOLOGY_FAIL_ESCALATION: IDOLOGY_FAIL_ESCALATION_SCENARIOS,
    IDOLOGY_PASS: IDOLOGY_PASS_SCENARIOS,
    LEXISNEXIS_HARD_BLOCK: LEXISNEXIS_HARD_BLOCK_SCENARIOS,
}


def register_all():
    for bundle_id, scenarios in BUNDLES.items():
        r = requests.post(f"{BASE}/register", json={"id": bundle_id, "scenarios": scenarios})
        r.raise_for_status()
        print(f"registered {bundle_id!r}: {r.json()}")


def implement(bundle_id: str):
    if bundle_id not in BUNDLES:
        print(f"unknown bundle {bundle_id!r}; known: {list(BUNDLES)}")
        sys.exit(1)
    r = requests.post(f"{BASE}/implement", json={"id": bundle_id})
    r.raise_for_status()
    print(f"implemented {bundle_id!r}: {r.json()}")
    active = requests.get(f"{BASE}/scenarios").json()
    print("active scenarios now:")
    for sc in active["scenarios"]:
        print(f"  - {sc['endpoint']} -> {sc['name']}")


def list_bundles():
    r = requests.get(f"{BASE}/register")
    r.raise_for_status()
    print(r.json())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "register"
    if cmd == "register":
        register_all()
    elif cmd == "implement":
        implement(sys.argv[2])
    elif cmd == "list":
        list_bundles()
    else:
        print(__doc__)
        sys.exit(1)
