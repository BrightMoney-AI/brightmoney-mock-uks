"""Generate data/pa_screening_cases.csv — PA (Patriot Act) screening test suite.

Authored against the real UKS AUT. The test flow graph is IDology-primary:
    idology (initial) --FAIL*--> lexisnexis --soft fail--> persona_idv

Written as a script so the CSV's quoting/column alignment is exact — the raw
XML/JSON mock bodies contain commas and quotes that are painful by hand.

ExpectID PA standalone is deliberately NOT mocked. That is fine and by design:
``screening_source`` is written when the row is CLAIMED, *before* any external
call, so the reuse-vs-call decision — the actual logic under test — is fully
verifiable without it. On the standalone path the call then fails to reach a
mock and lands ``screening_status=ERROR``, which is itself the proof that the
standalone branch was taken.
"""
import csv

HEADER = [
    "case_id", "layer", "kind", "flow_id", "tags",
    "seed.path", "seed.scenario", "seed.match", "seed.resp", "seed.is_sequence",
    "call.method", "call.url", "call.headers",
    "call.body.meta.bright_uid", "call.body.meta.request_id",
    "call.body.data.flow_id", "call.body.data.kyc_type", "call.body.data.client",
    "call.expect_status",
    "call2.method", "call2.url", "call2.headers", "call2.expect_status", "call2.delay_ms",
    "call2.body.meta.bright_uid", "call2.body.meta.request_id",
    "call2.body.data.flow_id", "call2.body.data.kyc_type",
    "call3.method", "call3.url", "call3.headers", "call3.expect_status", "call3.delay_ms",
    "call3.body.meta.bright_uid", "call3.body.meta.request_id",
    "call3.body.data.flow_id", "call3.body.data.kyc_type", "call3.body.data.client",
    "call3.body.data.in_sync",
    "call3.body.data.additional_data_params.persona_kyc_verified",
    "call3.body.data.additional_data_params.ssn",
    "call3.body.data.additional_data_params.address",
    "call3.body.data.additional_data_params.city",
    "call3.body.data.additional_data_params.state_short",
    "call3.body.data.additional_data_params.zip",
    "call4.method", "call4.url", "call4.headers", "call4.expect_status", "call4.delay_ms",
    "call4.body.meta.bright_uid", "call4.body.meta.request_id",
    "call4.body.data.flow_id", "call4.body.data.kyc_type", "call4.body.data.client",
    "call4.body.data.in_sync",
    "call4.body.data.additional_data_params.ssn",
    "call4.body.data.additional_data_params.address",
    "call4.body.data.additional_data_params.city",
    "call4.body.data.additional_data_params.state_short",
    "call4.body.data.additional_data_params.zip",
    "repeat.same_flow_id", "repeat.distinct_ids", "repeat.concurrent",
    "resp.status", "resp.body",
    "db.host", "db.database", "db.delay_ms",
    "db1.table", "db1.where", "db1.expect",
    "db2.table", "db2.where", "db2.expect",
    "db3.table", "db3.where", "db3.expect",
    "db4.table", "db4.where", "db4.expect",
    "calls", "notes",
]

DB = "brightmomey_uks_2"
# AUT base URL — change here only; every call column derives from it.
AUT = "http://10.0.63.105"
START = f"{AUT}/api/v1/kyc/start"
STATUS_DETAILS = f"{AUT}/api/v1/kyc/status_details/"
RESUME = f"{AUT}/api/v1/kyc/resume"
JSON_H = "Content-Type=application/json"
FLOW = "flow-{{uuid:flow}}"

# uks.models.KycFlowStatus values — the terminal success value is "PASSED", NOT
# "PASS". (data/kyc_cases.csv asserts status=PASS in 14 places; those assertions
# can never match and are silently broken.)
PASSED = "PASSED"
FAILED = "FAILED"

# --- mock bodies -----------------------------------------------------------
PROFILE = (
    'status=200;raw={"bright_uid":"{{uuid:uid}}","primary_email":"john@example.com",'
    '"primary_phonenum":"+11234567890","first_name":"John","last_name":"Doe",'
    '"date_of_birth":"1990-05-15","is_kyc_verified":false,"zip_code":"78701",'
    '"address":{"address_type":"bright","manual_address":{"apt":"","zip":"78701",'
    '"city":"Austin","state":{"long_name":"Texas","short_name":"TX"},'
    '"street":"123 Main St"}},"ssn_encrypted":"gAAAAABqQnJ4S7_gd7u2iaMV_8jNjzBVe4mk'
    'ZcJVYDdxDbjrKQj6cvjhPR9Avyo2VKbuE8efqeBjeZIZugYZY23qE8EoKeh7fg==",'
    '"ip":"192.168.1.1","age":35,"acquired_on":"IOS_APP","is_deleted":false,"error":{}}'
)

# IDology PASS, watch list clear (no <restriction>).
IDL_PASS_CLEAR = (
    'status=200;format=xml;raw=<?xml version="1.0"?><response>'
    "<id-number>3571500001</id-number>"
    "<summary-result><key>id.success</key><message>PASS</message></summary-result>"
    "<results><key>result.match</key><message>ID Located</message></results>"
    "</response>"
)
# IDology PASS *with* a PA hit. restriction_present does NOT alter the status
# (IdologyTriggerEvaluator keys only off summary-result), so the flow still
# PASSES via IDology while carrying a watch-list hit to be reused.
IDL_PASS_HIT = (
    'status=200;format=xml;raw=<?xml version="1.0"?><response>'
    "<id-number>3571500002</id-number>"
    "<summary-result><key>id.success</key><message>PASS</message></summary-result>"
    "<results><key>result.match</key><message>ID Located</message></results>"
    "<restriction><key>global.watch.list</key><message>Patriot Act Alert</message>"
    "<pa><list>OFAC SDN</list><score>100</score><record-type>Individual</record-type></pa>"
    "<pa><list>UK HM TREASURY LIST</list><score>100</score><record-type>Entity</record-type></pa>"
    "</restriction></response>"
)
# IDology FAIL, real response → PA screening still ran (pa_determined=True).
#
# The result code MUST be one the evaluator can categorise. dev's
# enrollment_waterfall graph only routes idology -> lexisnexis on the SPECIFIC
# categories (triggers 31-35: FAIL_SSN / FAIL_NAME / FAIL_DOB / FAIL_MOB /
# FAIL_ADDRESS) — there is NO trigger for plain "FAIL". "result.match" is in no
# FAIL_*_CODES set, so it collapses to plain FAIL, no trigger fires, and the flow
# dies FAILED at idology without ever calling LexisNexis. (prod's graph 4 DOES
# have a plain-FAIL trigger, which is what made this easy to get wrong —
# data/kyc_cases.csv relies on it and is therefore broken against dev.)
# resultcode.ssn.does.not.match is in FAIL_SSN_CODES -> FAIL_SSN -> trigger 31.
IDL_FAIL_CLEAR = (
    'status=200;format=xml;raw=<?xml version="1.0"?><response>'
    "<id-number>3571500003</id-number>"
    "<summary-result><key>id.failure</key><message>FAIL</message></summary-result>"
    "<results><key>resultcode.ssn.does.not.match</key>"
    "<message>SSN Does Not Match</message></results>"
    "</response>"
)
# IDology API-level error → parser sets .error → pa_determined=False (nothing
# was screened) AND status collapses to FAIL, so the graph still routes onward.
IDL_ERROR = (
    'status=200;format=xml;raw=<?xml version="1.0"?><response>'
    "<error>Internal service failure</error></response>"
)

LN_TOKEN = 'status=200;raw={"access_token":"tok_{{uuid:tok}}","expires_in":3600}'
# CVI 50 / low risk codes → LexisNexis PASS.
LN_PASS = (
    'status=200;raw={"Records":[{"InstantIDIndividual":'
    '{"ComprehensiveVerificationIndex":50,"NameAddressSSN":{"RiskCode":12},'
    '"NameAddressPhone":{"RiskCode":12}}}]}'
)
# CVI 10 / NameAddressSSN RiskCode 7 → non-SSN soft fail → routes to Persona.
LN_SOFT_FAIL = (
    'status=200;raw={"Records":[{"InstantIDIndividual":'
    '{"ComprehensiveVerificationIndex":10,"NameAddressSSN":{"RiskCode":7},'
    '"NameAddressPhone":{"RiskCode":12}}}]}'
)
# RiskIndicators code "IT" is in LexisNexisEvaluator's HARD_BLOCK_CODES, which
# short-circuits ahead of the CVI check → FAIL_HARD_BLOCK → flow FAILED.
# (A low CVI alone does NOT hard-block; this mirrors the existing TC-003 body.)
LN_HARD_BLOCK = (
    'status=200;raw={"Records":[{"InstantIDIndividual":'
    '{"ComprehensiveVerificationIndex":50,"NameAddressSSN":{"RiskCode":12},'
    '"NameAddressPhone":{"RiskCode":12},"RiskIndicators":[{"RiskCode":"IT"}]}}]}'
)
PERSONA_CREATE = (
    'status=200;raw={"data":{"id":"inq_{{uuid:inq}}","type":"inquiry",'
    '"attributes":{"status":"created","reference-id":"{{uuid:uid}}"}}}'
)

PA_TABLE = "kyc_pa_screening_results"
IDL_TABLE = "idology_kyc_verification"

# --- vendor paths the mock must serve --------------------------------------
# These are the paths the AUT actually POSTs to. Taken from the source of truth,
# NOT from data/kyc_cases.csv — that file seeds LexisNexis at "/api/Lists/Search"
# and "/api/OAuth2/Token", which are missing the "/LN.WebServices" prefix, so the
# mock has no endpoint there and every LexisNexis leg of those cases is dead.
P_PROFILE = "/api/v1/users/get_user_profile_data/"  # BM backend
P_IDOLOGY = "/vendor/idology/verify"  # settings.IDOLOGY_API_URL path
P_LN_TOKEN = "/LN.WebServices/api/OAuth2/Token"  # lexisnexis.constants.TOKEN_PATH
P_LN_SEARCH = "/LN.WebServices/api/Lists/Search"  # lexisnexis.constants.SEARCH_PATH
P_PERSONA = "/api/v1/inquiries"  # persona.constants.INQUIRIES_ENDPOINT


def row(**kw):
    r = dict.fromkeys(HEADER, "")
    r.update(kw)
    unknown = set(kw) - set(HEADER)
    assert not unknown, f"unknown columns: {unknown}"
    return r


def seed_row(case_id, path, scenario, resp):
    """A continuation row contributing one more seed to *case_id*."""
    return row(**{"case_id": case_id, "seed.path": path,
                  "seed.scenario": scenario, "seed.resp": resp})


def base(case_id, notes, seed_path, seed_scenario, seed_resp, *,
         db_delay=8000, tags="pa-screening"):
    """First row of a case: metadata + /start call + the first seed."""
    return row(**{
        "case_id": case_id, "layer": "5-BusinessE2E", "kind": "e2e",
        "flow_id": FLOW, "tags": tags,
        "seed.path": seed_path, "seed.scenario": seed_scenario, "seed.resp": seed_resp,
        "call.method": "POST", "call.url": START, "call.headers": JSON_H,
        "call.body.meta.bright_uid": "{{uuid:uid}}",
        "call.body.meta.request_id": "{{uuid:rid}}",
        "call.body.data.flow_id": FLOW,
        "call.body.data.kyc_type": "DM",
        "call.body.data.client": "app",
        "call.expect_status": "200",
        "resp.status": "200", "resp.body": "error=null;data.flow_id=not_null",
        "db.database": DB, "db.delay_ms": str(db_delay),
        "notes": notes,
    })


def add_status_details_then_resume(r, *, persona_verified="'PASS'", extra=None):
    """Attach call2=/status_details (opens Persona) and call3=/resume."""
    r.update({
        "call2.method": "POST", "call2.url": STATUS_DETAILS, "call2.headers": JSON_H,
        "call2.expect_status": "200", "call2.delay_ms": "3000",
        "call2.body.meta.bright_uid": "{{uuid:uid}}",
        "call2.body.meta.request_id": "{{uuid:rid_sd}}",
        "call2.body.data.flow_id": FLOW, "call2.body.data.kyc_type": "DM",
        "call3.method": "POST", "call3.url": RESUME, "call3.headers": JSON_H,
        "call3.expect_status": "200", "call3.delay_ms": "1500",
        "call3.body.meta.bright_uid": "{{uuid:uid}}",
        "call3.body.meta.request_id": "{{uuid:rid2}}",
        "call3.body.data.flow_id": FLOW, "call3.body.data.kyc_type": "DM",
        "call3.body.data.client": "USM", "call3.body.data.in_sync": "true",
        "call3.body.data.additional_data_params.persona_kyc_verified": persona_verified,
    })
    if extra:
        r.update(extra)
    return r


# NOTE on dbN slot ordering: the runner DELETEs each db_check's where-clause
# before the case runs, in slot order. kyc_pa_screening_results and
# idology_kyc_verification are children of uks_kyc_flow / uks_kyc_request under
# on_delete=PROTECT, so child tables MUST occupy lower slots than uks_kyc_flow —
# otherwise a re-run with a pinned flow_id would hit an FK violation on cleanup.
# (Fresh {{uuid:flow}} per run makes cleanup a no-op today; this keeps it correct
# if a flow_id is ever pinned.)
SLOT_PA = 1
SLOT_IDL = 2
SLOT_FLOW = 3

# On the standalone path ExpectID PA is intentionally unmocked, so the outcome
# depends on whether the AUT can reach the endpoint at all: unroutable → ERROR,
# reachable → COMPLETED. Either is acceptable; the assertion that actually
# matters is screening_source=PA_STANDALONE, which is written at claim time
# BEFORE the call and therefore proves the decision independent of the network.
STANDALONE_STATUS = "/^(ERROR|COMPLETED)$/"


def pa_checks(r, *, source, status="COMPLETED", hit=None, review=None,
              provider=None, request_id=None, extra=(), slot=SLOT_PA):
    """Add the kyc_pa_screening_results expectation (one check per table)."""
    exp = [f"screening_source={source}", f"screening_status={status}"]
    if hit is not None:
        exp.append(f"pa_hit={hit}")
    if review is not None:
        exp.append(f"pa_review_required={review}")
    if provider is not None:
        exp.append(f"primary_provider={provider}")
    if request_id is not None:
        exp.append(f"pa_request_id={request_id}")
    exp.append("pa_pii_fingerprint=not_null")
    exp.extend(extra)
    r.update({
        f"db{slot}.table": PA_TABLE,
        f"db{slot}.where": f"flow_id={FLOW}",
        f"db{slot}.expect": ";".join(exp),
    })
    return r


def pa_absent(r, slot=SLOT_PA):
    r.update({f"db{slot}.table": PA_TABLE, f"db{slot}.where": f"flow_id={FLOW}",
              f"db{slot}.expect": "__absent__=true"})
    return r


def idl_check(r, expect, slot=SLOT_IDL):
    r.update({f"db{slot}.table": IDL_TABLE, f"db{slot}.where": f"flow_id={FLOW}",
              f"db{slot}.expect": expect})
    return r


def flow_check(r, status, slot=SLOT_FLOW):
    r.update({f"db{slot}.table": "uks_kyc_flow",
              f"db{slot}.where": f"flow_id={FLOW}",
              f"db{slot}.expect": f"status={status}"})
    return r


CALLS_IDL_ONLY = f"{P_PROFILE}=1;{P_IDOLOGY}=1"
CALLS_IDL_LN = CALLS_IDL_ONLY + f";{P_LN_SEARCH}=1"
# ">=1" must NOT be preceded by "=" — parse_calls_cell's greedy regex would fold
# the "=" into the path and the assertion could never match.
CALLS_IDL_LN_PERSONA = CALLS_IDL_LN + f";{P_PERSONA}>=1"

rows = []

# =========================================================================
# PA-001 — IDology PASS, watch list clear, PII unchanged -> REUSE, no hit
# =========================================================================
r = base("PA-001", "IDology PASS + clear watch list -> reuse IQ screening, no standalone call.",
         P_IDOLOGY, "idology-pass-clear", IDL_PASS_CLEAR)
pa_checks(r, source="IDOLOGY_IQ_REUSED", hit="False", review="False",
          provider="IDOLOGY", request_id="3571500001")
idl_check(r, "pa_determined=True;pa_restriction_present=False;pa_pii_fingerprint=not_null")
flow_check(r, PASSED)
r["calls"] = CALLS_IDL_ONLY
rows += [r, seed_row("PA-001", P_PROFILE, "usm-profile-default", PROFILE)]

# =========================================================================
# PA-002 — IDology PASS *with* <restriction> -> REUSE, hit surfaced
# =========================================================================
r = base("PA-002", "IDology PASS carrying a PA hit -> reuse marks pa_hit + review_required.",
         P_IDOLOGY, "idology-pass-hit", IDL_PASS_HIT)
pa_checks(r, source="IDOLOGY_IQ_REUSED", hit="True", review="True",
          provider="IDOLOGY", request_id="3571500002",
          extra=("pa_hit_details=/OFAC SDN/", "source_verification_pid=not_null"))
idl_check(r, "pa_determined=True;pa_restriction_present=True")
flow_check(r, PASSED)
r["calls"] = CALLS_IDL_ONLY
rows += [r, seed_row("PA-002", P_PROFILE, "usm-profile-default", PROFILE)]

# =========================================================================
# PA-003 — IDology FAIL -> LexisNexis PASS, PII unchanged -> REUSE
#          (the headline case: passed by a DIFFERENT provider, still reused)
# =========================================================================
r = base("PA-003", "IDology FAIL then LexisNexis PASS, PII unchanged -> reuse IDology's screening.",
         P_IDOLOGY, "idology-fail-clear", IDL_FAIL_CLEAR)
pa_checks(r, source="IDOLOGY_IQ_REUSED", hit="False",
          provider="LEXISNEXIS", request_id="3571500003")
idl_check(r, "pa_determined=True")
flow_check(r, PASSED)
r["calls"] = CALLS_IDL_LN
rows += [
    r,
    seed_row("PA-003", P_LN_TOKEN, "ln-token", LN_TOKEN),
    seed_row("PA-003", P_LN_SEARCH, "ln-pass", LN_PASS),
    seed_row("PA-003", P_PROFILE, "usm-profile-default", PROFILE),
]

# PA-004 — IDology API error -> pa_determined=False, flow dies at idology
#          On enrollment_waterfall an <error> collapses to plain FAIL, and there
#          is NO plain-FAIL trigger, so the flow can never reach LexisNexis.
#          What IS assertable (and valuable): the errored call is recorded with
#          pa_determined=False so it can never satisfy a future reuse check, and
#          a FAILED flow gets no screening row.
# =========================================================================
r = base("PA-004",
         "IDology API error -> pa_determined=False recorded; FAILED flow gets no screening.",
         P_IDOLOGY, "idology-api-error", IDL_ERROR)
flow_check(r, FAILED, slot=SLOT_PA)
pa_absent(r, slot=SLOT_IDL)
idl_check(r, "pa_determined=False;pa_restriction_present=False", slot=SLOT_FLOW)
r["calls"] = CALLS_IDL_ONLY
rows += [r, seed_row("PA-004", P_PROFILE, "usm-profile-default", PROFILE)]

# =========================================================================
# PA-005 — escalation round-trip with a PII correction -> REUSE
#
#   idology(FAIL_SSN, PII_A) -> lexisnexis(FAIL_NON_SSN) -> persona(PASS)
#     -> ESCALATE_SSN parks back at idology
#     -> /resume #2 supplies corrected ssn + address (PII_B)
#     -> idology re-runs (sequence: 2nd response PASSES) -> terminal PASSED
#
# The 2nd idology call screens PII_B, which IS what the flow passed with, so the
# reuse check matches it — proving reuse tracks the latest matching call, not the
# first. Terminal provider is IDOLOGY (the escalation target on this graph).
# =========================================================================
r = base("PA-005",
         "Escalation round-trip: PII corrected at resume, idology re-screens it -> reuse.",
         P_IDOLOGY, "idology-fail-then-pass", IDL_FAIL_CLEAR, db_delay=12000)
add_status_details_then_resume(r)
r.update({
    "call4.method": "POST", "call4.url": RESUME, "call4.headers": JSON_H,
    "call4.expect_status": "200", "call4.delay_ms": "3000",
    "call4.body.meta.bright_uid": "{{uuid:uid}}",
    "call4.body.meta.request_id": "{{uuid:rid3}}",
    "call4.body.data.flow_id": FLOW, "call4.body.data.kyc_type": "DM",
    "call4.body.data.client": "USM", "call4.body.data.in_sync": "true",
    "call4.body.data.additional_data_params.ssn": "'987654321'",
    "call4.body.data.additional_data_params.address": "999 Different Ave",
    "call4.body.data.additional_data_params.city": "Dallas",
    "call4.body.data.additional_data_params.state_short": "TX",
    "call4.body.data.additional_data_params.zip": "75201",
})
pa_checks(r, source="IDOLOGY_IQ_REUSED", provider="IDOLOGY")
flow_check(r, PASSED)
# idology runs TWICE here (initial call + the post-escalation re-run), so the
# count differs from every other case.
r["calls"] = (
    f"{P_PROFILE}=1;{P_IDOLOGY}=2;{P_LN_SEARCH}=1;{P_PERSONA}>=1"
)
rows += [
    r,
    # 2nd row for the SAME (path, scenario) makes this a SEQUENCE: call 1 fails
    # with FAIL_SSN, call 2 (after the escalation resume) passes.
    seed_row("PA-005", P_IDOLOGY, "idology-fail-then-pass", IDL_PASS_CLEAR),
    seed_row("PA-005", P_LN_TOKEN, "ln-token", LN_TOKEN),
    seed_row("PA-005", P_LN_SEARCH, "ln-non-ssn-soft-fail", LN_SOFT_FAIL),
    seed_row("PA-005", P_PERSONA, "persona-create", PERSONA_CREATE),
    seed_row("PA-005", P_PROFILE, "usm-profile-default", PROFILE),
]

# =========================================================================
# =========================================================================
# PA-008 — FAILED flow -> NO screening row at all (negative case)
# =========================================================================
r = base("PA-008", "LexisNexis hard block -> flow FAILED -> PA screening must never run.",
         P_IDOLOGY, "idology-fail-clear", IDL_FAIL_CLEAR)
# Order matters for a negative assertion: poll uks_kyc_flow until it is
# actually FAILED (slot 1) BEFORE the single-shot absent check (slot 2), so
# "no screening row" cannot pass merely because nothing has happened yet.
flow_check(r, FAILED, slot=SLOT_PA)
pa_absent(r, slot=SLOT_IDL)
r["calls"] = CALLS_IDL_LN
rows += [
    r,
    seed_row("PA-008", P_LN_TOKEN, "ln-token", LN_TOKEN),
    seed_row("PA-008", P_LN_SEARCH, "ln-hard-block", LN_HARD_BLOCK),
    seed_row("PA-008", P_PROFILE, "usm-profile-default", PROFILE),
]

# =========================================================================
# PA-009 — Persona declined -> flow FAILED -> NO screening row (negative)
# =========================================================================
r = base("PA-009", "Persona /resume verified FALSE -> flow FAILED -> PA screening must never run.",
         P_IDOLOGY, "idology-fail-clear", IDL_FAIL_CLEAR)
add_status_details_then_resume(r, persona_verified="'FALSE'")
# Order matters for a negative assertion: poll uks_kyc_flow until it is
# actually FAILED (slot 1) BEFORE the single-shot absent check (slot 2), so
# "no screening row" cannot pass merely because nothing has happened yet.
flow_check(r, FAILED, slot=SLOT_PA)
pa_absent(r, slot=SLOT_IDL)
r["calls"] = CALLS_IDL_LN_PERSONA
rows += [
    r,
    seed_row("PA-009", P_LN_TOKEN, "ln-token", LN_TOKEN),
    seed_row("PA-009", P_LN_SEARCH, "ln-non-ssn-soft-fail", LN_SOFT_FAIL),
    seed_row("PA-009", P_PERSONA, "persona-create", PERSONA_CREATE),
    seed_row("PA-009", P_PROFILE, "usm-profile-default", PROFILE),
]

# =========================================================================
# PA-010 — replaying /start with the same flow_id yields ONE screening row
#          (flow_id is the /start idempotency key; the screening row is
#          UNIQUE(kyc_flow) so a replay can never double-screen)
# =========================================================================
r = base("PA-010", "Replayed /start on one flow_id -> single screening row, source still reuse.",
         P_IDOLOGY, "idology-pass-clear", IDL_PASS_CLEAR)
pa_checks(r, source="IDOLOGY_IQ_REUSED", hit="False", provider="IDOLOGY")
flow_check(r, PASSED)
r.update({"repeat.same_flow_id": "3", "calls": CALLS_IDL_ONLY})
rows += [r, seed_row("PA-010", P_PROFILE, "usm-profile-default", PROFILE)]


out = "data/pa_screening_cases.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)
print(f"wrote {out}: {len(rows)} rows, {len({r['case_id'] for r in rows})} cases")
