#!/usr/bin/env bash
# End-to-end demo: starts the mock server (:8000) and the dummy AUT (:8001),
# then runs the CSV-driven test runner over data/demo_cases.csv.
#
# The dummy AUT reads MOCK_BASE_URL to reach the mock; the runner verifies the
# AUT's own sqlite database and the mock's CallLog.
set -euo pipefail
cd "$(dirname "$0")"

export DJANGO_SETTINGS_MODULE=config.settings
: "${MOCKVENDOR_DB_DIR:=$PWD}"
export MOCKVENDOR_DB_DIR
AUT_DB="$MOCKVENDOR_DB_DIR/aut.sqlite3"

python3 manage.py migrate >/dev/null
python3 manage.py migrate --database=aut >/dev/null

# Mock server on :8000
python3 manage.py runserver 127.0.0.1:8000 --noreload >/tmp/mock.log 2>&1 &
MOCK_PID=$!
# Dummy AUT on :8001 (same project; it just exposes /aut/enroll and calls the mock)
MOCK_BASE_URL=http://127.0.0.1:8000 \
  python3 manage.py runserver 127.0.0.1:8001 --noreload >/tmp/aut.log 2>&1 &
AUT_PID=$!
trap 'kill $MOCK_PID $AUT_PID 2>/dev/null || true' EXIT

sleep 3
python3 -m testrunner data/demo_cases.csv \
  --mock-base http://127.0.0.1:8000 \
  --aut-sqlite "$AUT_DB"
