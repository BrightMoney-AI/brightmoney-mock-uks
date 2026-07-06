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

from .models import TestResult, TestRun

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
        "id": run.id, "csv": run.csv_path, "tag": run.tag, "case_filter": run.case_filter,
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
             "errors": r.errors, "duration_ms": r.duration_ms}
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
        limit = min(int(request.query_params.get("limit", 50) or 50), 500)
        runs = TestRun.objects.all()[:limit]
        return DrfResponse({"runs": [_run_json(r) for r in runs]})

    # POST: start a run
    data = request.data if isinstance(request.data, dict) else {}
    csv_p = _resolve_csv(data.get("csv", ""))
    if not csv_p:
        return DrfResponse({"error": "csv must name a .csv file under data/"},
                           status=status.HTTP_400_BAD_REQUEST)
    active = TestRun.objects.filter(status__in=ACTIVE).first()
    if active:
        return DrfResponse(
            {"error": f"a run is already active (#{active.id}, {active.status}); "
                      "runs are serialized to avoid scenario collisions"},
            status=status.HTTP_409_CONFLICT)

    run = TestRun.objects.create(
        csv_path=str(csv_p.relative_to(settings.BASE_DIR)),
        tag=(data.get("tag") or "").strip(),
        case_filter=(data.get("cases") or "").strip(),
        mock_base=(data.get("mock_base") or "http://127.0.0.1").strip().rstrip("/"),
        created_ip=_client_ip(request), status="pending",
    )
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
