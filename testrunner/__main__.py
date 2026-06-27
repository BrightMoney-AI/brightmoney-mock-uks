"""CLI: python -m testrunner cases.csv --mock-base http://127.0.0.1:8000 [--aut-sqlite aut.sqlite3]

Modes:
  --validate-only   parse + run §12.3 MUST checks on every row, no AUT calls.
  (default)         seed -> drive -> verify -> cleanup each case against a live
                    mock server and AUT, printing a pass/fail report.
"""
from __future__ import annotations

import argparse
import sys

from . import runner, schema


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="testrunner")
    ap.add_argument("cases_csv")
    ap.add_argument("--mock-base", default="http://127.0.0.1:8000")
    ap.add_argument("--aut-sqlite", default=None,
                    help="Verify DB checks against this sqlite file (demo AUT).")
    ap.add_argument("--enable-kafka", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--tag", default=None, help="Only run cases carrying this tag.")
    args = ap.parse_args(argv)

    cases = runner.load_cases(args.cases_csv)
    if args.tag:
        cases = [c for c in cases if args.tag in c.tags]

    if args.validate_only:
        bad = 0
        for c in cases:
            errs = schema.validate(c)
            status = "OK  " if not errs else "FAIL"
            if errs:
                bad += 1
            print(f"[{status}] {c.case_id}" + (f"  -> {'; '.join(errs)}" if errs else ""))
        print(f"\n{len(cases) - bad}/{len(cases)} rows valid.")
        return 1 if bad else 0

    run = runner.Runner(args.mock_base, aut_sqlite=args.aut_sqlite, enable_kafka=args.enable_kafka)
    passed = 0
    for c in cases:
        res = run.run_case(c)
        if res.passed:
            passed += 1
        status = "PASS" if res.passed else "FAIL"
        print(f"[{status}] {c.case_id}" + (f"  -> {'; '.join(res.errors)}" if res.errors else ""))
    print(f"\n{passed}/{len(cases)} cases passed.")
    return 1 if passed != len(cases) else 0


if __name__ == "__main__":
    sys.exit(main())
