"""Durable, immutable replay records, including the exact fitted inputs."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.model import CashflowModel, Decision, DecisionDelta, SimResult
from data.money import USD, USD_CENTS


class _PgConnection:
    """Gives a psycopg connection the sqlite3 calls RunStore uses (? placeholders, INSERT OR IGNORE, executemany)."""

    def __init__(self, connection):
        self._connection = connection

    @staticmethod
    def _translate(sql: str) -> str:
        sql = sql.replace("?", "%s")
        if sql.lstrip().upper().startswith("INSERT OR IGNORE"):
            sql = sql.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT DO NOTHING"
        return sql

    def execute(self, sql: str, params=()):
        return self._connection.execute(self._translate(sql), params)

    def executemany(self, sql: str, rows):
        with self._connection.cursor() as cursor:
            cursor.executemany(self._translate(sql), list(rows))


class RunStore:
    """SQLite file by default; Postgres (Supabase) when `database_url` is given."""

    def __init__(self, path: Path, database_url: str | None = None):
        self.path = path
        self.database_url = database_url
        if not database_url:
            path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                result_json TEXT NOT NULL,
                model_json TEXT NOT NULL,
                delta_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS transactions (
                txn_id TEXT PRIMARY KEY, record_json TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS transaction_units (
                txn_id TEXT PRIMARY KEY, monetary_unit TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS run_units (
                run_id TEXT PRIMARY KEY, units_json TEXT NOT NULL
            )""")

    @contextmanager
    def connect(self):
        if self.database_url:
            import psycopg
            with psycopg.connect(self.database_url, connect_timeout=10) as connection:
                yield _PgConnection(connection)
            return
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, result: SimResult, model: CashflowModel, delta: DecisionDelta | Decision, response: dict,
             evidence: dict[str, dict], *, evidence_units: dict[str, str] | None = None) -> None:
        encode = lambda value: json.dumps(value, allow_nan=False, separators=(",", ":"))
        units = evidence_units if evidence_units is not None else {tid: USD for tid in evidence}
        if set(units) != set(evidence) or any(unit not in (USD, USD_CENTS) for unit in units.values()):
            raise ValueError("Evidence monetary units must be complete and known")
        with self.connect() as connection:
            for tid, raw in evidence.items():
                old = connection.execute("SELECT record_json FROM transactions WHERE txn_id = ?", (tid,)).fetchone()
                if old and (json.loads(old[0]) != raw or self._transaction_unit(connection, tid) != units[tid]):
                    raise ValueError("Archived transaction record or monetary unit conflicts")
            connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)", (
                result.run_id, datetime.now(timezone.utc).isoformat(), encode(result.to_dict()),
                encode(model.to_dict()), encode(delta.to_dict()), encode(response), encode(evidence)))
            connection.executemany("INSERT OR IGNORE INTO transactions VALUES (?, ?)",
                                   [(tid, encode(raw)) for tid, raw in evidence.items()])
            connection.executemany("INSERT OR IGNORE INTO transaction_units VALUES (?, ?)", units.items())
            connection.execute("INSERT INTO run_units VALUES (?, ?)", (
                result.run_id, encode({"model": USD, "delta": USD, "evidence": units})))

    @staticmethod
    def _transaction_unit(connection, txn_id: str) -> str:
        row = connection.execute("SELECT monetary_unit FROM transaction_units WHERE txn_id = ?", (txn_id,)).fetchone()
        return row[0] if row else USD  # Original saved runs used dollar records.

    def transaction_unit(self, txn_id: str) -> str:
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone():
                raise KeyError(txn_id)
            return self._transaction_unit(connection, txn_id)

    def transaction(self, txn_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT record_json FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
        if row is None:
            raise KeyError(txn_id)
        return json.loads(row[0])

    def get(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT result_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def recent(self, customer_id: str, limit: int = 30) -> list[dict]:
        """A customer's latest replays, newest first: whatever channel asked them (web, iMessage, API)."""
        with self.connect() as connection:
            rows = connection.execute("SELECT run_id, created_at, model_json, delta_json, response_json FROM runs "
                                      "ORDER BY created_at DESC LIMIT 2000").fetchall()
        found = []
        for run_id, created_at, model_json, delta_json, response_json in rows:
            if json.loads(model_json).get("customer_id") != customer_id:
                continue
            delta, response = json.loads(delta_json), json.loads(response_json or "{}")
            question = delta.get("raw_text") or next((i.get("raw_text") for i in delta.get("items", []) if i.get("raw_text")), "")
            verdict = (response.get("prose") or "").split("\n\n")[0].strip()
            found.append({"run_id": run_id, "created_at": created_at, "question": question, "verdict": verdict,
                          "mode": response.get("mode", "decision")})
            if len(found) == limit:
                break
        return found

    def inputs(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT model_json, delta_json, evidence_json, created_at, response_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            units = connection.execute("SELECT units_json FROM run_units WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        evidence = json.loads(row[2])
        response = json.loads(row[4]) if row[4] else {}
        stored = json.loads(row[1])
        decision = Decision.from_dict(stored)  # old runs stored one flat delta
        return {"model": json.loads(row[0]), "delta": decision.items[0].to_dict(), "decision": decision.to_dict(), "evidence": evidence,
                "created_at": row[3], "prose": response.get("prose"),
                "amount_units": json.loads(units[0]) if units else {
                    "model": USD, "delta": USD, "evidence": {tid: USD for tid in evidence}}}
