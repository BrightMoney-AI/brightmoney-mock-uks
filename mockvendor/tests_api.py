"""Dashboard test-runner API (design §8 companion).

  GET  /mock/admin/test-csvs          list runnable CSV suites under data/
  GET  /mock/admin/testruns           recent runs (newest first)
  POST /mock/admin/testruns           start a run: {csv, tag?, cases?, mock_base?}
  GET  /mock/admin/testruns/{id}      run detail + per-case results
  DELETE /mock/admin/testruns/{id}    delete a run + its results

A run is executed by the ``run_testsuite`` management command spawned as a
detached subprocess, so it outlives gunicorn worker recycling. Runs are
serialized: the CSV runner seeds the *default* scenario namespace and drives the
AUT (whose callbacks carry no run_id), so two concurrent suites would corrupt
each other's scenarios — a new run is refused while one is active.
"""
from __future__ import annotations

import csv as _csv
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response as DrfResponse

from .models import TestCase, TestResult, TestRun
from .pagination import page_envelope, parse_page_params
from testrunner import runner as trunner
from testrunner import schema as tschema

DATA_DIR = (Path(settings.BASE_DIR) / "data").resolve()
ACTIVE = ("pending", "running")


def _enabled() -> bool:
    return bool(getattr(settings, "MOCKVENDOR_ADMIN_ENABLED", False))


def _forbidden():
    return DrfResponse({"error": "mock admin API disabled"}, status=status.HTTP_403_FORBIDDEN)


def _client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")) or ""


def _resolve_csv(name: str) -> Path | None:
    """Map a user-supplied csv name to a real file under data/ (no traversal)."""
    if not name:
        return None
    rel = name if name.startswith("data/") else f"data/{os.path.basename(name)}"
    p = (Path(settings.BASE_DIR) / rel).resolve()
    if DATA_DIR not in p.parents or not p.is_file() or p.suffix != ".csv":
        return None
    return p


def _run_json(run: TestRun, with_results: bool = False) -> dict:
    d = {
        "id": run.id, "source": run.source, "csv": run.csv_path,
        "suite": run.suite, "case_ids": run.case_ids,
        "tag": run.tag, "case_filter": run.case_filter,
        "mock_base": run.mock_base, "status": run.status, "total": run.total,
        "passed": run.passed, "failed": run.failed, "skipped": run.skipped,
        "error": run.error, "created_ip": run.created_ip,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if with_results:
        d["results"] = [
            {"case_id": r.case_id, "passed": r.passed, "skipped": r.skipped,
             "errors": r.errors, "responses": r.responses, "duration_ms": r.duration_ms}
            for r in run.results.all()
        ]
    return d


def _spawn(run_id: int) -> tuple[int, str]:
    """Launch the run in a detached subprocess; return (pid, log_path-relative)."""
    logdir = Path(settings.BASE_DIR) / "logs" / "testruns"
    logdir.mkdir(parents=True, exist_ok=True)
    logpath = logdir / f"run_{run_id}.log"
    logf = open(logpath, "ab")  # closed by the child; parent fd released on return
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "run_testsuite", str(run_id)],
        cwd=str(settings.BASE_DIR), stdout=logf, stderr=logf,
        start_new_session=True, env={**os.environ},
    )
    logf.close()
    try:
        rel = str(logpath.relative_to(settings.BASE_DIR))
    except ValueError:
        rel = str(logpath)
    return proc.pid, rel


@api_view(["GET"])
def test_csvs(request):
    if not _enabled():
        return _forbidden()
    out = []
    for p in sorted(DATA_DIR.glob("*.csv")):
        if p.name == "kyc_scenarios.csv":  # scenario-library CSV, not a case suite
            continue
        try:
            with p.open(newline="", encoding="utf-8") as f:
                reader = _csv.DictReader(f)
                ids = {(row.get("case_id") or "").strip() for row in reader}
                ids.discard("")
                cases = len(ids)
        except Exception:
            cases = None
        out.append({"name": p.name, "cases": cases})
    return DrfResponse({"csvs": out})


@api_view(["GET", "POST"])
def testruns(request):
    if not _enabled():
        return _forbidden()
    if request.method == "GET":
        limit, offset = parse_page_params(request, default_limit=50, max_limit=500)
        qs = TestRun.objects.all()  # already ordered "-id" via Meta.ordering
        total = qs.count()
        page = list(qs[offset:offset + limit])
        return DrfResponse({"runs": [_run_json(r) for r in page],
                            **page_envelope(total, offset, limit, len(page))})

    # POST: start a run (source "csv" | "db")
    data = request.data if isinstance(request.data, dict) else {}
    source = (data.get("source") or "csv").strip()
    common = dict(
        tag=(data.get("tag") or "").strip(),
        case_filter=(data.get("cases") or "").strip(),
        mock_base=(data.get("mock_base") or "http://127.0.0.1").strip().rstrip("/"),
        created_ip=_client_ip(request), status="pending",
    )
    if source == "db":
        case_ids = data.get("case_ids") or []
        suite = (data.get("suite") or "").strip()
        if not case_ids and not suite:
            return DrfResponse({"error": "db run needs 'suite' or 'case_ids'"},
                               status=status.HTTP_400_BAD_REQUEST)
        create_kw = dict(source="db", suite=suite, case_ids=list(case_ids), **common)
    else:
        csv_p = _resolve_csv(data.get("csv", ""))
        if not csv_p:
            return DrfResponse({"error": "csv must name a .csv file under data/"},
                               status=status.HTTP_400_BAD_REQUEST)
        create_kw = dict(source="csv", csv_path=str(csv_p.relative_to(settings.BASE_DIR)), **common)

    active = TestRun.objects.filter(status__in=ACTIVE).first()
    if active:
        return DrfResponse(
            {"error": f"a run is already active (#{active.id}, {active.status}); "
                      "runs are serialized to avoid scenario collisions"},
            status=status.HTTP_409_CONFLICT)

    run = TestRun.objects.create(**create_kw)
    try:
        pid, log_path = _spawn(run.id)
        run.pid, run.log_path = pid, log_path
        run.save(update_fields=["pid", "log_path"])
    except Exception as exc:
        run.status, run.error = "error", f"failed to spawn runner: {exc}"
        run.save(update_fields=["status", "error"])
        return DrfResponse({"error": run.error}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return DrfResponse(_run_json(run), status=status.HTTP_201_CREATED)


@api_view(["GET", "DELETE"])
def testrun_detail(request, run_id: int):
    if not _enabled():
        return _forbidden()
    try:
        run = TestRun.objects.get(pk=run_id)
    except TestRun.DoesNotExist:
        return DrfResponse({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        run.delete()
        return DrfResponse(status=status.HTTP_204_NO_CONTENT)
    return DrfResponse(_run_json(run, with_results=True))


# ---------------------------------------------------------------------------
# Editable DB-stored test cases (visual editor backing store)
# ---------------------------------------------------------------------------
def _case_json(tc: TestCase, full: bool = False) -> dict:
    d = {"id": tc.id, "case_id": tc.case_id, "suite": tc.suite, "tags": tc.tags,
         "enabled": tc.enabled, "notes": tc.notes,
         "modified_at": tc.modified_at.isoformat()}
    if full:
        d["definition"] = tc.definition
    else:
        defn = tc.definition or {}
        d["summary"] = {
            "seeds": len(defn.get("seeds", []) or []),
            "call": (defn.get("call", {}) or {}).get("url", ""),
            "steps": len(defn.get("call_steps", []) or []),
            "expects": len((defn.get("resp", {}) or {}).get("body", {}) or {})
                       + len(defn.get("db_checks", []) or [])
                       + len(defn.get("calls", {}) or {}),
        }
    return d


@api_view(["GET", "POST"])
def testcases(request):
    if not _enabled():
        return _forbidden()
    if request.method == "GET":
        qs = TestCase.objects.all()  # already ordered (suite, case_id) via Meta.ordering
        suite = request.query_params.get("suite")
        if suite is not None:
            qs = qs.filter(suite=suite)
        suites = sorted({s for s in TestCase.objects.values_list("suite", flat=True)})
        limit, offset = parse_page_params(request, default_limit=200, max_limit=1000)
        total = qs.count()
        page = list(qs[offset:offset + limit])
        return DrfResponse({"cases": [_case_json(t) for t in page], "suites": suites,
                            **page_envelope(total, offset, limit, len(page))})

    # POST: create one case
    data = request.data if isinstance(request.data, dict) else {}
    defn = data.get("definition") or {}
    case_id = (data.get("case_id") or defn.get("case_id") or "").strip()
    if not case_id:
        return DrfResponse({"error": "case_id required"}, status=status.HTTP_400_BAD_REQUEST)
    defn["case_id"] = case_id
    tc = TestCase.objects.create(
        case_id=case_id, suite=(data.get("suite") or "").strip(),
        definition=defn, tags=defn.get("tags", []) or [], notes=defn.get("notes", "") or "",
        enabled=bool(data.get("enabled", True)),
    )
    return DrfResponse(_case_json(tc, full=True), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
def testcase_detail(request, case_pk: int):
    if not _enabled():
        return _forbidden()
    try:
        tc = TestCase.objects.get(pk=case_pk)
    except TestCase.DoesNotExist:
        return DrfResponse({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        tc.delete()
        return DrfResponse(status=status.HTTP_204_NO_CONTENT)
    if request.method == "PUT":
        data = request.data if isinstance(request.data, dict) else {}
        defn = data.get("definition")
        if defn is not None:
            if data.get("case_id"):
                defn["case_id"] = data["case_id"].strip()
            tc.definition = defn
            tc.tags = defn.get("tags", tc.tags) or []
            tc.notes = defn.get("notes", tc.notes) or ""
        if "case_id" in data:
            tc.case_id = data["case_id"].strip()
        if "suite" in data:
            tc.suite = (data["suite"] or "").strip()
        if "enabled" in data:
            tc.enabled = bool(data["enabled"])
        tc.save()
    return DrfResponse(_case_json(tc, full=True))


@api_view(["POST"])
def testcases_import(request):
    """Import a suite into editable TestCase rows (templates preserved).

    Body is one of:
      {csv, suite?}                    — import an existing file under data/
      {filename, content, suite?}      — upload a CSV from the caller's own
                                          machine: it is first WRITTEN under
                                          data/ (so it also shows up in
                                          /test-csvs and can be re-imported /
                                          run as a CSV suite later), then
                                          imported exactly like the first form.

    suite defaults to the csv stem. Re-importing updates existing
    (suite, case_id) rows rather than duplicating them.
    """
    if not _enabled():
        return _forbidden()
    data = request.data if isinstance(request.data, dict) else {}

    if data.get("content") is not None:
        raw_name = os.path.basename((data.get("filename") or "upload.csv").strip()) or "upload.csv"
        if not raw_name.lower().endswith(".csv"):
            raw_name += ".csv"
        dest = DATA_DIR / raw_name
        # Write to a fresh temp file and rename it into place, rather than
        # truncating `dest` in place: an in-place write needs write permission
        # on that exact inode, which a same-named file from an earlier deploy
        # (e.g. checked out as a different user) may not grant this process —
        # os.replace() only needs write permission on the containing directory,
        # the same guarantee `git checkout` relies on for tracked files.
        tmp = dest.with_name(f".{dest.name}.upload-{os.getpid()}.tmp")
        try:
            tmp.write_text(data["content"], encoding="utf-8")
            os.replace(tmp, dest)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            return DrfResponse({"error": f"could not save upload: {exc}"},
                               status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        csv_p = dest
    else:
        csv_p = _resolve_csv(data.get("csv", ""))
        if not csv_p:
            return DrfResponse(
                {"error": "csv must name a .csv file under data/, or supply {filename, content}"},
                status=status.HTTP_400_BAD_REQUEST)

    suite = (data.get("suite") or csv_p.stem).strip()
    with csv_p.open(newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        if not tschema.is_new_format(list(reader.fieldnames or [])):
            return DrfResponse({"error": "only the seed.* CSV format is importable"},
                               status=status.HTTP_400_BAD_REQUEST)
        groups: dict[str, list[dict]] = {}
        for row in reader:
            cid = (row.get("case_id") or "").strip()
            if cid:
                cur = cid
                groups.setdefault(cur, []).append(row)
            elif groups:
                list(groups.values())[-1].append(row)
    if not groups:
        return DrfResponse({"error": "no case_id rows found in this CSV"},
                           status=status.HTTP_400_BAD_REQUEST)
    n = 0
    for cid, rows in groups.items():
        case = tschema.parse_case_new(rows, interpolate=False)  # keep {{uuid}} templates
        defn = tschema.case_to_dict(case)
        TestCase.objects.update_or_create(
            suite=suite, case_id=cid,
            defaults={"definition": defn, "tags": defn.get("tags", []) or [],
                      "notes": defn.get("notes", "") or "", "enabled": True})
        n += 1
    return DrfResponse({"suite": suite, "imported": n, "csv": csv_p.name})


@api_view(["POST"])
def testcase_validate(request):
    """Dry-run the runner's MUST-rule validation on a definition (no AUT calls)."""
    if not _enabled():
        return _forbidden()
    data = request.data if isinstance(request.data, dict) else {}
    try:
        case = tschema.case_from_dict(data.get("definition") or {})
        errors = tschema.validate(case)
    except Exception as exc:
        return DrfResponse({"ok": False, "errors": [f"parse error: {exc}"]})
    return DrfResponse({"ok": not errors, "errors": errors})
