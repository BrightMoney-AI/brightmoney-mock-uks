"""Turn every case in the CSV test suites into a registered mock scenario bundle.

Instead of hand-transcribing individual cases (error-prone, doesn't scale),
this reuses the same parser the test runner itself uses
(testrunner.schema.parse_case_new) to read data/test_suite_full.csv and
data/kyc_cases.csv, then registers one bundle per case_id via the mockvendor
admin API (mockvendor/urls.py, mockvendor/admin_api.py):

  POST /mock/admin/register    save + seed a named bundle: {"id", "scenarios":[...]}
  POST /mock/admin/implement   reset scenarios (CallLog untouched), replay by id: {"id"}

Bundle id = "<file-stem>:<case_id lowercased>", e.g. "test_suite_full:ido-03",
"test_suite_full:esc-ssn-diff", "kyc_cases:tc-004". Namespaced by file because
both CSVs define TC-001..TC-026 against different vendor URL conventions
(kyc_cases.csv: /api/OAuth2/Token; test_suite_full.csv: /LN.WebServices/api/OAuth2/Token)
-- a bare case_id would silently pick one file's version and drop the other.
"implement"-ing a bundle swaps every vendor mock over to that case's responses
in a single call, without touching CallLog.

Usage:
    python register_case_bundles.py register                       # register every case as a bundle
    python register_case_bundles.py register data/kyc_cases.csv    # just one file
    python register_case_bundles.py implement test_suite_full:ido-03  # swap mocks to that case
    python register_case_bundles.py list                           # every bundle id available
"""
from __future__ import annotations

import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testrunner.schema import SeedGroup, parse_case_new  # noqa: E402

BASE = "http://127.0.0.1:8000/mock/admin"
REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CSVS = [REPO_ROOT / "data" / "test_suite_full.csv", REPO_ROOT / "data" / "kyc_cases.csv"]


def _group_rows_by_case(path: Path) -> dict[str, list[dict]]:
    """Rows sharing a case_id form one case: the first row (non-blank case_id)
    carries the metadata; subsequent blank-case_id rows contribute extra seeds
    only, exactly as the runner interprets data/*.csv (see README §12)."""
    groups: dict[str, list[dict]] = {}
    current_id = None
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cid = (row.get("case_id") or "").strip()
            if cid:
                current_id = cid
                groups.setdefault(current_id, []).append(row)
            elif current_id and (row.get("seed.path") or "").strip():
                # continuation row: reuse the case_id so parse_case_new sees one group
                row = dict(row)
                row["case_id"] = current_id
                groups[current_id].append(row)
    return groups


def _seed_group_to_payload(sg: SeedGroup) -> dict:
    responses = sg.responses or [sg.resp]
    return {
        "method": sg.method,
        "path": sg.path,
        "default_format": (responses[0].get("format") if responses else None) or "json",
        "scenario": sg.scenario,
        "priority": sg.priority,
        "is_sequence": sg.is_sequence,
        "match_key": sg.match_key,
        "match_value": sg.match_value,
        "responses": [
            {
                "status": r.get("status", 200),
                "format": r.get("format"),
                "raw": r.get("raw", ""),
                "canonical": r.get("canonical") or None,
                "delay_ms": r.get("delay_ms", 0),
            }
            for r in responses
        ],
    }


def build_bundles(csv_paths: list[Path]) -> dict[str, list[dict]]:
    """Return {bundle_id: [seed_scenario_dict payload, ...]} across all given CSVs.

    Bundle id is namespaced as "<file-stem>:<case_id>" (e.g. "kyc_cases:tc-001").
    Case ids collide across data/test_suite_full.csv and data/kyc_cases.csv (both
    define TC-001..TC-026, pointed at *different* vendor URL conventions —
    /api/OAuth2/Token vs /LN.WebServices/api/OAuth2/Token) — merging them under a
    bare case_id would silently drop one file's version. Namespacing keeps both.
    """
    bundles: dict[str, list[dict]] = {}
    seen_case_ids: dict[str, str] = {}
    for path in csv_paths:
        for case_id, rows in _group_rows_by_case(path).items():
            case = parse_case_new(rows)
            if not case.seeds:
                continue
            bundle_id = f"{path.stem}:{case_id.lower()}"
            bundles[bundle_id] = [_seed_group_to_payload(sg) for sg in case.seeds]
            if case_id.lower() in seen_case_ids and seen_case_ids[case_id.lower()] != path.stem:
                print(f"note: case_id {case_id!r} appears in both "
                      f"{seen_case_ids[case_id.lower()]}.csv and {path.stem}.csv -> "
                      f"kept as separate bundles ({seen_case_ids[case_id.lower()]}:{case_id.lower()}, "
                      f"{bundle_id})", file=sys.stderr)
            seen_case_ids[case_id.lower()] = path.stem
    return bundles


def _register_one(session: requests.Session, bundle_id: str, scenarios: list[dict]):
    r = session.post(f"{BASE}/register", json={"id": bundle_id, "scenarios": scenarios}, timeout=30)
    r.raise_for_status()
    return bundle_id


def register_all(csv_paths: list[Path], max_workers: int = 16):
    """POST each bundle concurrently — every bundle is an independent DB write
    (its own Endpoint/Scenario rows, see mockvendor/seed.py:_upsert), so there's
    no ordering dependency between bundles and threads are safe. Django's dev
    server handles concurrent requests by default (runserver is threaded)."""
    bundles = build_bundles(csv_paths)
    ok, failed = 0, []
    with requests.Session() as session, ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_register_one, session, bundle_id, scenarios): bundle_id
            for bundle_id, scenarios in bundles.items()
        }
        for future in as_completed(futures):
            bundle_id = futures[future]
            try:
                future.result()
                ok += 1
            except requests.HTTPError as exc:
                failed.append((bundle_id, exc.response.status_code, exc.response.text))
            except requests.RequestException as exc:
                failed.append((bundle_id, "conn-error", str(exc)))
    print(f"registered {ok}/{len(bundles)} bundles from {[str(p) for p in csv_paths]} "
          f"({max_workers} concurrent workers)")
    for bundle_id, status, text in failed:
        print(f"  FAILED {bundle_id}: {status} {text[:200]}")


def implement(bundle_id: str):
    r = requests.post(f"{BASE}/implement", json={"id": bundle_id})
    if r.status_code == 404:
        print(f"unknown bundle {bundle_id!r} (register first)")
        sys.exit(1)
    r.raise_for_status()
    print(f"implemented {bundle_id!r}: {r.json()}")
    active = requests.get(f"{BASE}/scenarios").json()
    print("active scenarios now:")
    for sc in active["scenarios"]:
        print(f"  - {sc['endpoint']} -> {sc['name']}")


def list_bundles(csv_paths: list[Path]):
    bundles = build_bundles(csv_paths)
    for bundle_id, scenarios in sorted(bundles.items()):
        paths = sorted({s["path"] for s in scenarios})
        print(f"{bundle_id:30s} {len(scenarios)} scenario(s): {', '.join(paths)}")
    print(f"\n{len(bundles)} total case bundles available")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "register"
    extra_args = sys.argv[2:]
    if cmd == "register":
        csvs = [Path(a) for a in extra_args] if extra_args else DEFAULT_CSVS
        register_all(csvs)
    elif cmd == "implement":
        implement(extra_args[0])
    elif cmd == "list":
        csvs = [Path(a) for a in extra_args] if extra_args else DEFAULT_CSVS
        list_bundles(csvs)
    else:
        print(__doc__)
        sys.exit(1)
