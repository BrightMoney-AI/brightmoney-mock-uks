"""Execute a persisted TestRun by id, writing per-case results to the DB.

Invoked as a detached subprocess by the dashboard API (mockvendor/tests_api.py)
so a full suite survives gunicorn worker recycling. The TestRun row is the
source of truth for progress; each case's result is committed as it finishes so
the dashboard can stream progress by polling.

    python manage.py run_testsuite <run_id>
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from mockvendor.models import TestCase, TestResult, TestRun
from testrunner import runner as trunner
from testrunner import schema as tschema


class Command(BaseCommand):
    help = "Execute a persisted TestRun (by id) and store per-case results."

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)

    def handle(self, *args, **opts):
        run_id = opts["run_id"]
        try:
            run = TestRun.objects.get(pk=run_id)
        except TestRun.DoesNotExist:
            self.stderr.write(f"no TestRun {run_id}")
            return

        run.status = "running"
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])

        try:
            if run.source == "db":
                qs = TestCase.objects.filter(enabled=True)
                if run.suite:
                    qs = qs.filter(suite=run.suite)
                if run.case_ids:
                    qs = qs.filter(id__in=run.case_ids)
                tcs = list(qs)
                if run.case_filter.strip():
                    wanted = {x.strip() for x in run.case_filter.replace("\n", ",").split(",") if x.strip()}
                    tcs = [t for t in tcs if t.case_id in wanted]
                cases = [tschema.case_from_dict(t.definition) for t in tcs]
                src = f"db suite={run.suite!r} ids={run.case_ids or 'all'}"
            else:
                csv_abs = (Path(settings.BASE_DIR) / run.csv_path).resolve()
                cases = trunner.load_cases(str(csv_abs))
                if run.tag:
                    cases = [c for c in cases if run.tag in c.tags]
                if run.case_filter.strip():
                    wanted = {x.strip() for x in run.case_filter.replace("\n", ",").split(",") if x.strip()}
                    cases = [c for c in cases if c.case_id in wanted]
                src = run.csv_path

            run.total = len(cases)
            run.save(update_fields=["total"])
            self.stdout.write(f"run {run_id}: {len(cases)} case(s) from {src} "
                              f"(tag={run.tag!r} filter={run.case_filter!r}) mock_base={run.mock_base}")

            r = trunner.Runner(run.mock_base)
            passed = failed = skipped = 0
            for c in cases:
                t0 = time.monotonic()
                try:
                    res = r.run_case(c)
                    dur = int((time.monotonic() - t0) * 1000)
                    is_skip = getattr(res, "skipped", False)
                    TestResult.objects.create(
                        run=run, case_id=res.case_id, passed=res.passed,
                        skipped=is_skip, errors=res.errors or [], duration_ms=dur)
                    if is_skip:
                        skipped += 1
                    elif res.passed:
                        passed += 1
                    else:
                        failed += 1
                    tag = "SKIP" if is_skip else ("PASS" if res.passed else "FAIL")
                except Exception as exc:  # a single case must not abort the whole run
                    dur = int((time.monotonic() - t0) * 1000)
                    TestResult.objects.create(
                        run=run, case_id=c.case_id, passed=False,
                        errors=[f"runner exception: {exc}"], duration_ms=dur)
                    failed += 1
                    tag = "ERR "
                self.stdout.write(f"[{tag}] {c.case_id}")
                run.passed, run.failed, run.skipped = passed, failed, skipped
                run.save(update_fields=["passed", "failed", "skipped"])

            run.status = "passed" if failed == 0 else "failed"
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at"])
            self.stdout.write(f"run {run_id}: {passed}/{run.total} passed, {failed} failed, {skipped} skipped")
        except Exception:
            run.status = "error"
            run.error = traceback.format_exc()[-6000:]
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error", "finished_at"])
            self.stderr.write(run.error)
