# Vendor Mock & Test Framework

A configurable **mock server** plus a **CSV-driven test runner** for simulating
internal & external vendor services — the reference implementation of
*Mock Server Design v6*.

Two cooperating components, both standard Django:

| Component | What it is |
|---|---|
| **`mockvendor`** | A reusable Django app that returns predefined vendor responses (JSON/XML), with controllable status codes, latency (`delay_ms`), stateful sequences, and discriminator-based scenario selection. All scenarios are DB rows. |
| **`testrunner`** | A standalone, **CSV-driven** runner. One CSV row per test case: it seeds the mock, drives the Application-Under-Test (AUT), and verifies the AUT's own database, the mock's CallLog, and (optionally) Kafka. |

A tiny **`dummy_aut`** app ships too — a stand-in KYC service so you can run the
whole loop end-to-end with zero external infrastructure.

---

## Quick start

```bash
pip install -r requirements.txt

python manage.py migrate                 # mock server DB
python manage.py migrate --database=aut   # dummy AUT DB

# Run all unit tests (mock server + schema parser)
pytest

# End-to-end demo: starts mock (:8000) + dummy AUT (:8001), runs the demo suite
./run_demo.sh
```

`run_demo.sh` prints a pass/fail line per case:

```
[PASS] DEMO-001
...
8/8 cases passed.
```

> **SQLite on a network/FUSE mount?** If `migrate` reports a "disk I/O error",
> point the DB elsewhere: `export MOCKVENDOR_DB_DIR=/tmp/vmf`. On a normal local
> disk this is not needed.

---

## The mock server (`mockvendor`)

Mounted by `config/urls.py`:

- `POST /<any vendor path>` — the **catch-all serve view**. Looks up the endpoint,
  selects the scenario (discriminator → priority), advances any sequence cursor,
  applies `delay_ms`, serializes the canonical body, returns it, logs the call.
- `/mock/admin/...` — the **admin/seed API** (design §8), gated on
  `MOCKVENDOR_ADMIN_ENABLED` (defaults to `DEBUG`):

| Endpoint | Method | Purpose |
|---|---|---|
| `/mock/admin/scenarios` | POST | Create / seed a scenario (single object or list) |
| `/mock/admin/scenarios` | GET | List active scenarios |
| `/mock/admin/scenarios/{id}` | PUT/DELETE | Update / remove |
| `/mock/admin/reset` | POST | Clear scenarios + CallLog (test isolation) |
| `/mock/admin/calls` | GET | CallLog rows + per-path counts (for assertions) |
| `/mock/admin/formats` | GET | Registered format serializers |

- `/admin/` — Django admin, with a `ModelAdmin` for every model.

### Data model (design §4)

`Format`, `Endpoint`, `Scenario`, `Response`, `CallLog`. Stateful sequences are
multiple `Response` rows under one `Scenario`, ordered by `seq_index` — no
separate sequence table.

### Adding a format (design §3.4)

Write one `FormatSerializer` class and add one line to
`settings.MOCKVENDOR_SERIALIZERS`. JSON and XML ship in
`mockvendor/serializers_fmt.py`. For byte-exact / malformed payloads use a
response's `raw_override` instead of `canonical`.

### Seeding from git

```bash
python manage.py seed_scenarios data/kyc_scenarios.csv
```

`kyc_scenarios.csv` is a "scenario library" (one `Response` row per line); rows
sharing a `(path, scenario)` become a sequence.

---

## The test runner (`testrunner`)

```bash
# Validate every row against the §12.3 MUST rules (no AUT calls):
python -m testrunner data/kyc_cases.csv --validate-only

# Run a suite against a live mock + AUT, verifying the AUT's sqlite DB:
python -m testrunner data/demo_cases.csv \
    --mock-base http://127.0.0.1:8000 \
    --aut-sqlite ./aut.sqlite3

# Filter by tag; enable Kafka checks (needs kafka-python):
python -m testrunner data/demo_cases.csv --tag happy --enable-kafka
```

Per case the runner does **seed → drive → verify → cleanup** (design §6.3):
seeds each `seedN.*` group via the admin API, drives the AUT (POST by default,
with `repeat.*` for replay / distinct-id / concurrency load), then checks the
AUT response, the AUT database (`dbN.*`), the mock CallLog (`calls`), and Kafka
(`kafkaN.*`), and finally resets the mock.

### CSV schema (design §12)

One header row, one row per case. Dotted prefixes group columns; small maps live
in one cell as `key=value;key=value` (escape a literal `;`/`,` with `\`).

```
case_id, flow_id, tags, client_context,
seedN.path, seedN.method, seedN.scenario, seedN.priority, seedN.match, seedN.resp, seedN.is_sequence,
call.method, call.url, call.headers, call.body.*, call.expect_status,
repeat.same_flow_id, repeat.distinct_ids, repeat.concurrent,
resp.status, resp.body,
db.host, db.database, dbN.table, dbN.where, dbN.expect,
kafka.bootstrap, kafkaN.topic, kafkaN.key, kafkaN.expect,
calls
```

`expect` values support `not_null` and `/regex/`.

---

## Bundled data (`data/`)

| File | What it is |
|---|---|
| `demo_cases.csv` | 8 cases tuned to the bundled `dummy_aut`; what `run_demo.sh` runs. |
| `kyc_scenarios.csv` | Scenario-library seed file for `seed_scenarios`. |
| `kyc_cases.csv` | The full 26-case KYC suite authored against the **real** UKS AUT (IDology → LexisNexis → Persona → escalations). Validates with `--validate-only`; point `--mock-base`/`--aut-sqlite` at a real deployment to run it. |

---

## Layout

```
vendor-mock-framework/
├── manage.py                 config/  (settings, urls, wsgi, db router)
├── mockvendor/               the reusable mock server app
│   ├── models.py  matcher.py  views.py  serializers_fmt.py
│   ├── seed.py  admin_api.py  admin.py  urls.py
│   ├── management/commands/seed_scenarios.py
│   └── tests/test_mock.py
├── testrunner/               the CSV-driven runner
│   ├── schema.py  runner.py  verifiers.py  __main__.py
│   └── tests/test_schema.py
├── dummy_aut/                stand-in AUT for the end-to-end demo
├── data/                     seed + case CSVs
└── run_demo.sh
```
