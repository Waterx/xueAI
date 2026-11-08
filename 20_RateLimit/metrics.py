"""保存每次模型调用的观测数据，并从同一份记录计算汇总。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DB_PATH = Path(__file__).resolve().with_name("metrics.db")
RequestStatus = Literal["success", "error", "cancelled"]


@dataclass
class RequestMetrics:
    model: str
    stream: bool
    latency_ms: float
    status: RequestStatus
    ttft_ms: float | None = None
    usage: dict[str, int] | None = None
    request_id: str | None = None
    conversation_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def metric_filters(request_id, conversation_id, before_id=None):
    filters = []
    parameters = []
    for clause, value in (
        ("request_id = ?", request_id),
        ("conversation_id = ?", conversation_id),
        ("id < ?", before_id),
    ):
        if value is not None:
            filters.append(clause)
            parameters.append(value)
    return (" WHERE " + " AND ".join(filters) if filters else ""), parameters


class MetricsStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS request_metrics (
                    id INTEGER PRIMARY KEY,
                    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    model TEXT NOT NULL,
                    stream INTEGER NOT NULL CHECK (stream IN (0, 1)),
                    status TEXT NOT NULL CHECK (status IN ('success', 'error', 'cancelled')),
                    latency_ms REAL NOT NULL,
                    ttft_ms REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    request_id TEXT,
                    conversation_id TEXT,
                    usage_json TEXT,
                    error_code TEXT,
                    error_message TEXT
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(request_metrics)")}
            if "request_id" not in columns:
                connection.execute("ALTER TABLE request_metrics ADD COLUMN request_id TEXT")
            if "conversation_id" not in columns:
                connection.execute("ALTER TABLE request_metrics ADD COLUMN conversation_id TEXT")
            if "usage_json" not in columns:
                connection.execute("ALTER TABLE request_metrics ADD COLUMN usage_json TEXT")
            if "error_code" not in columns:
                connection.execute("ALTER TABLE request_metrics ADD COLUMN error_code TEXT")
            if "error_message" not in columns:
                connection.execute("ALTER TABLE request_metrics ADD COLUMN error_message TEXT")
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS request_metrics_request_id
                ON request_metrics(request_id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS request_metrics_conversation_id
                ON request_metrics(conversation_id, id)
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, request: RequestMetrics) -> None:
        usage = request.usage or {}
        usage_json = json.dumps(request.usage) if request.usage is not None else None
        with closing(self._connect()) as connection, connection:
            connection.execute("""
                INSERT INTO request_metrics (
                    model, stream, status, latency_ms, ttft_ms,
                    input_tokens, output_tokens, total_tokens, request_id, conversation_id, usage_json,
                    error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request.model, request.stream, request.status, request.latency_ms,
                request.ttft_ms, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                usage.get("total_tokens"), request.request_id, request.conversation_id, usage_json,
                request.error_code, request.error_message,
            ))

    def snapshot(
        self, request_id: str | None = None, conversation_id: str | None = None,
    ) -> dict[str, dict[str, int | float | None]]:
        summary = {}
        where, parameters = metric_filters(request_id, conversation_id)
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT model,
                       COUNT(*) AS total_requests,
                       SUM(status = 'success') AS success_requests,
                       SUM(status = 'error') AS error_requests,
                       SUM(status = 'cancelled') AS cancelled_requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       AVG(latency_ms) AS avg_latency_ms,
                       AVG(ttft_ms) AS avg_ttft_ms
                FROM request_metrics
            """ + where + " GROUP BY model", parameters)
            for row in rows:
                stats = dict(row)
                model = stats.pop("model")
                summary[model] = stats
        return summary

    def recent(
        self, limit: int, before_id: int | None,
        request_id: str | None = None, conversation_id: str | None = None,
    ) -> list[dict]:
        where, parameters = metric_filters(request_id, conversation_id, before_id)
        records = []
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT id, completed_at, model, stream, status, latency_ms, ttft_ms,
                       input_tokens, output_tokens, total_tokens, request_id, conversation_id, usage_json,
                       error_code, error_message
                FROM request_metrics
            """ + where + " ORDER BY id DESC LIMIT ?", [*parameters, limit])
            for row in rows:
                record = dict(row, stream=bool(row["stream"]))
                usage_json = record.pop("usage_json")
                record["usage"] = json.loads(usage_json) if usage_json is not None else None
                records.append(record)
        return records
