#!/usr/bin/env bash
# Run the full 8-case demo (AUT + mock) against a local PostgreSQL database.
#
# Prerequisites:
#   1. ./setup.sh already run with DB_ENGINE=django.db.backends.postgresql in .env
#   2. Both mockvendor and mockvendor_aut databases exist and are migrated
#
# The script:
#   - Starts the mock server on :8000
#   - Starts the dummy AUT on :8001 (same Django project, reads MOCK_BASE_URL)
#   - Runs the CSV test runner against data/demo_cases_pg.csv
#     (db.host=localhost:5432, db.database=mockvendor_aut)
#   - Verifies AUT DB rows via psycopg (uses PGUSER/PGPASSWORD forwarded from .env)
set -euo pipefail
cd "$(dirname "$0")"

source venv/bin/activate

if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

export DJANGO_SETTINGS_MODULE=config.settings

# Forward DB credentials so psycopg (used by the test runner's verifier) can
# connect to the AUT database without needing a password in the CSV.
export PGUSER="${DB_USER:-$USER}"
export PGPASSWORD="${DB_PASSWORD:-}"
export PGHOST="${AUT_DB_HOST:-${DB_HOST:-localhost}}"
export PGPORT="${AUT_DB_PORT:-${DB_PORT:-5432}}"

# Mock server on :8000
python manage.py runserver 127.0.0.1:8000 --noreload >/tmp/mock.log 2>&1 &
MOCK_PID=$!

# Dummy AUT on :8001 — same project, different port, points at the mock
MOCK_BASE_URL=http://127.0.0.1:8000 \
  python manage.py runserver 127.0.0.1:8001 --noreload >/tmp/aut.log 2>&1 &
AUT_PID=$!

trap 'kill $MOCK_PID $AUT_PID 2>/dev/null || true' EXIT

echo "Waiting for servers to start..."
sleep 3

echo "Running demo cases against PostgreSQL (data/demo_cases_pg.csv)..."
python -m testrunner data/demo_cases_pg.csv \
    --mock-base http://127.0.0.1:8000
