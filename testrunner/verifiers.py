"""Verification surfaces (design §6.3, §11.4): AUT database, Kafka, mock CallLog."""
from __future__ import annotations

import os
import re
import sqlite3
import time

import requests


def _expect_match(actual, expected: str) -> bool:
    if expected == "not_null":
        return actual is not None and actual != ""
    if expected == "null":
        return actual is None
    if len(expected) >= 2 and expected.startswith("/") and expected.endswith("/"):
        return actual is not None and re.search(expected[1:-1], str(actual)) is not None
    return str(actual) == expected


# Flow-scoped UKS tables have no ``flow_id`` column — they key off ``kyc_flow_id``
# (FK to uks_kyc_flow) or ``step_pid``. A ``flow_id=`` filter on these is rewritten
# into the right sub-select so cases can be written uniformly with flow_id.
_FLOW_JOIN = {
    "uks_flow_decision_step": "kyc_flow_id IN (SELECT id FROM uks_kyc_flow WHERE flow_id={ph})",
    "uks_user_profile": "kyc_flow_id IN (SELECT id FROM uks_kyc_flow WHERE flow_id={ph})",
    "uks_kyc_data_fetch": "kyc_flow_id IN (SELECT id FROM uks_kyc_flow WHERE flow_id={ph})",
    "escalation_log": ("step_pid IN (SELECT pid FROM uks_flow_decision_step WHERE "
                       "kyc_flow_id IN (SELECT id FROM uks_kyc_flow WHERE flow_id={ph}))"),
}


def _build_clause(table: str, where: dict, ph: str) -> tuple[str, list]:
    """Build a WHERE clause + ordered params, rewriting ``flow_id`` on flow-scoped
    tables into a join sub-select. ``ph`` is the placeholder: ``?`` (sqlite) / ``%s`` (pg)."""
    if not where:
        return ("1=1" if ph == "?" else "TRUE"), []
    parts, params = [], []
    for k, v in where.items():
        if k == "flow_id" and table in _FLOW_JOIN:
            parts.append(_FLOW_JOIN[table].format(ph=ph))
        else:
            parts.append(f"{k}={ph}")
        params.append(v)
    return " AND ".join(parts), params


# ---------------------------------------------------------------------------
# Database cleanup (pre-test teardown of leftover AUT rows)
# ---------------------------------------------------------------------------
def cleanup_db_sqlite(sqlite_path: str, table: str, where: dict) -> None:
    con = sqlite3.connect(sqlite_path)
    try:
        clause, params = _build_clause(table, where, "?")
        con.execute(f"DELETE FROM {table} WHERE {clause}", params)
        con.commit()
    finally:
        con.close()


def cleanup_db_postgres(dsn: str, database: str, table: str, where: dict) -> None:
    try:
        import psycopg  # type: ignore
    except ImportError:
        return
    host, _, port = dsn.partition(":")
    with psycopg.connect(host=host, port=port or 5432, dbname=database,
                         user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD")) as con:  # pragma: no cover
        with con.cursor() as cur:
            clause, params = _build_clause(table, where, "%s")
            cur.execute(f"DELETE FROM {table} WHERE {clause}", params)
        con.commit()


# ---------------------------------------------------------------------------
# Database verifier
# ---------------------------------------------------------------------------
def verify_db_sqlite(sqlite_path: str, table: str, where: dict, expect: dict,
                     timeout_s: float = 20.0, poll_interval_s: float = 2.0) -> list[str]:
    deadline = time.monotonic() + timeout_s
    while True:
        errs: list[str] = []
        con = sqlite3.connect(sqlite_path)
        con.row_factory = sqlite3.Row
        try:
            clause, params = _build_clause(table, where, "?")
            rows = con.execute(f"SELECT * FROM {table} WHERE {clause}", params).fetchall()
            if not rows:
                errs = [f"db: no row in {table} where {where}"]
            else:
                row = rows[0]
                for col, exp in expect.items():
                    actual = row[col] if col in row.keys() else None
                    if not _expect_match(actual, exp):
                        errs.append(f"db: {table}.{col} expected {exp!r}, got {actual!r}")
        finally:
            con.close()
        if not errs or time.monotonic() >= deadline:
            return errs
        time.sleep(poll_interval_s)


def verify_db_postgres(dsn: str, database: str, table: str, where: dict, expect: dict,
                       timeout_s: float = 20.0, poll_interval_s: float = 2.0) -> list[str]:
    try:
        import psycopg  # type: ignore
    except ImportError:
        return ["db: psycopg not installed — install 'psycopg[binary]' or use --aut-sqlite"]
    host, _, port = dsn.partition(":")
    deadline = time.monotonic() + timeout_s
    while True:
        errs: list[str] = []
        with psycopg.connect(host=host, port=port or 5432, dbname=database,
                             user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD")) as con:  # pragma: no cover
            with con.cursor() as cur:
                clause, params = _build_clause(table, where, "%s")
                cur.execute(f"SELECT * FROM {table} WHERE {clause}", params)
                cols = [d.name for d in cur.description]
                r = cur.fetchone()
                if not r:
                    errs = [f"db: no row in {table} where {where}"]
                else:
                    row = dict(zip(cols, r))
                    for col, exp in expect.items():
                        if not _expect_match(row.get(col), exp):
                            errs.append(f"db: {table}.{col} expected {exp!r}, got {row.get(col)!r}")
        if not errs or time.monotonic() >= deadline:
            return errs
        time.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# CallLog reader (via the mock admin API)
# ---------------------------------------------------------------------------
def verify_calls(mock_base: str, expected: dict, baseline: dict | None = None,
                 timeout_s: float = 15.0, poll_interval_s: float = 1.0) -> list[str]:
    """Poll the CallLog until all expected deltas are met or timeout expires.

    The UKS processes KYC asynchronously — mock calls arrive seconds after
    drive() returns. Polling avoids a false FAIL due to this delay.
    """
    baseline = baseline or {}
    url = mock_base.rstrip("/") + "/mock/admin/calls"
    deadline = time.monotonic() + timeout_s
    while True:
        counts = requests.get(url, timeout=10).json().get("counts", {})
        errs = []
        for path, want in expected.items():
            got = counts.get(path, 0) - baseline.get(path, 0)
            if got != want:
                errs.append(f"calls: {path} expected {want}, got {got}")
        if not errs or time.monotonic() >= deadline:
            return errs
        time.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Kafka verifier (optional)
# ---------------------------------------------------------------------------
def verify_kafka(bootstrap: str, topic: str, key: str, expect, timeout_s: float = 5.0) -> list[str]:
    try:
        from kafka import KafkaConsumer  # type: ignore
    except ImportError:
        return ["kafka: kafka-python not installed — skipping (install kafka-python to enable)"]
    import json  # pragma: no cover
    consumer = KafkaConsumer(  # pragma: no cover
        topic, bootstrap_servers=bootstrap, auto_offset_reset="earliest",
        consumer_timeout_ms=int(timeout_s * 1000), value_deserializer=lambda b: b,
    )
    found = []  # pragma: no cover
    for msg in consumer:  # pragma: no cover
        if key and msg.key and msg.key.decode() != key:
            continue
        try:
            found.append(json.loads(msg.value))
        except ValueError:
            found.append({"_raw": msg.value.decode("utf-8", "replace")})
    consumer.close()  # pragma: no cover
    if expect == "absent":  # pragma: no cover
        return [] if not found else [f"kafka: expected no message on {topic}, got {len(found)}"]
    for ev in found:  # pragma: no cover
        if all(_expect_match(ev.get(k), v) for k, v in expect.items()):
            return []
    return [f"kafka: no message on {topic} matching {expect}"]  # pragma: no cover
