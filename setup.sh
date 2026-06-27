#!/usr/bin/env bash
# Bootstrap the vendor-mock-framework for local development.
#
# What this does:
#   1. Creates a Python venv at ./venv and installs requirements.txt
#   2. Loads .env to detect database engine
#   3. If PostgreSQL: creates the two databases (mockvendor + mockvendor_aut)
#   4. Runs Django migrations for both databases
#
# Usage:
#   ./setup.sh                     # SQLite (default, zero infrastructure)
#
#   # PostgreSQL — edit .env first (uncomment DB_ENGINE block), then:
#   ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

# ── 1. Python venv ────────────────────────────────────────────────────────────
# Django 5 requires Python 3.10+. Prefer python3.12 if available.
PYTHON=$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)
PY_VERSION=$("$PYTHON" -c "import sys; print(sys.version_info[:2])")
if [[ "$PY_VERSION" < "(3, 10)" ]]; then
    echo "ERROR: Django 5 requires Python 3.10+. Found $("$PYTHON" --version)."
    echo "Install Python 3.12 (e.g. brew install python@3.12) and retry."
    exit 1
fi

if [ ! -d "venv" ]; then
    "$PYTHON" -m venv venv
    echo "Created venv using $("$PYTHON" --version)"
fi
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "Dependencies installed"

# ── 2. Load .env ──────────────────────────────────────────────────────────────
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

export DJANGO_SETTINGS_MODULE=config.settings

# ── 3. Create PostgreSQL databases (if engine is postgresql) ──────────────────
if [ "${DB_ENGINE:-}" = "django.db.backends.postgresql" ]; then
    PG_HOST="${DB_HOST:-localhost}"
    PG_PORT="${DB_PORT:-5432}"
    PG_USER="${DB_USER:-$USER}"
    PG_MOCK_DB="${DB_NAME:-mockvendor}"
    PG_AUT_DB="${AUT_DB_NAME:-mockvendor_aut}"

    echo "Creating PostgreSQL databases on ${PG_HOST}:${PG_PORT} as ${PG_USER}..."

    createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_MOCK_DB" 2>/dev/null \
        && echo "  Created $PG_MOCK_DB" \
        || echo "  $PG_MOCK_DB already exists — skipping"

    createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_AUT_DB" 2>/dev/null \
        && echo "  Created $PG_AUT_DB" \
        || echo "  $PG_AUT_DB already exists — skipping"
fi

# ── 4. Migrations ─────────────────────────────────────────────────────────────
echo "Running migrations..."
python manage.py migrate --run-syncdb
python manage.py migrate --database=aut --run-syncdb
echo "Migrations done"

echo ""
echo "Setup complete. Next steps:"
echo ""
echo "  Activate venv:        source venv/bin/activate"
echo ""
echo "  Run demo (SQLite):    ./run_demo.sh"
echo "  Run demo (direct mock, no AUT):"
echo "    source venv/bin/activate && python manage.py runserver 127.0.0.1:8000 --noreload &"
echo "    sleep 2 && python -m testrunner data/demo_mock_direct.csv --mock-base http://127.0.0.1:8000"
echo ""
echo "  Run demo (PostgreSQL, full AUT flow):  ./run_demo_pg.sh"
