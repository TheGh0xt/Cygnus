"""Layer 3 memory store — Postgres.

Same contract as SqliteMemoryStore, backed by the Supabase Postgres this
project already uses for accounts. The two are interchangeable: SQLite stays
the zero-infrastructure choice for local development and tests, Postgres is
what a deployed instance uses.

This exists because a hosted instance cannot keep the store on local disk.
Free container platforms give you an ephemeral filesystem, so every restart
would start with an empty database — and each row holds the price observed at
the moment a report was written. That price cannot be reconstructed later; it
is the baseline Layer 5 scores against 48 hours on. Losing it means there is
no accuracy record to publish, which is the product's whole claim.

Reached over PostgREST with the service role key, matching how accounts are
accessed, so there is no second database dependency or connection pool to
manage.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from ..schemas.report import MarketAnalysisReport
from .store import StoredReport

logger = logging.getLogger("cygnus.memory.postgres")

_TABLE = "/analysis_reports"


class MemoryStoreError(Exception):
    """Any failure talking to the report store."""


class PostgresMemoryStore:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self._service_key = service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not (self._base_url and self._service_key):
            raise MemoryStoreError(
                "PostgresMemoryStore needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
            )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {
            "apikey": self._service_key,
            "authorization": f"Bearer {self._service_key}",
            "content-type": "application/json",
            **kwargs.pop("headers", {}),
        }
        try:
            response = httpx.request(
                method,
                f"{self._base_url}/rest/v1{path}",
                headers=headers,
                timeout=20,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise MemoryStoreError(f"report store unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise MemoryStoreError(
                f"report store returned {response.status_code}: {response.text[:200]}"
            )
        return response

    def save_report(
        self,
        report: MarketAnalysisReport,
        market_slug: str,
        price_at_report: float | None,
        created_at: datetime | None = None,
    ) -> int:
        created = created_at or datetime.now(tz=UTC)
        rows = self._request(
            "POST",
            _TABLE,
            headers={"prefer": "return=representation"},
            json={
                "market_id": report.market_id,
                "market_slug": market_slug,
                # model_dump(mode="json") rather than model_dump_json(): the
                # column is jsonb, so it wants a real object, not a string
                # containing JSON. Sending a string would store a quoted blob
                # that every reader then has to parse twice.
                "report_json": report.model_dump(mode="json"),
                "confidence_score": report.confidence_score,
                "price_at_report": price_at_report,
                "created_at": created.isoformat(),
            },
        ).json()
        return int(rows[0]["id"])

    def get_reports_due_for_evaluation(
        self, now: datetime, min_age_hours: int = 48
    ) -> list[StoredReport]:
        cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
        rows = self._request(
            "GET",
            _TABLE,
            params={
                "evaluated_at": "is.null",
                "created_at": f"lte.{cutoff}",
                "select": "*",
                "order": "created_at.asc",
            },
        ).json()
        return [self._to_stored(row) for row in rows]

    def record_evaluation(
        self,
        report_id: int,
        new_confidence: float,
        outcome: str,
        evaluated_at: datetime,
    ) -> None:
        rows = self._request(
            "GET",
            _TABLE,
            params={"id": f"eq.{report_id}", "select": "report_json", "limit": 1},
        ).json()
        if not rows:
            # Same contract as the SQLite store: a missing id is a caller bug,
            # not a silent no-op.
            raise KeyError(f"no stored report with id {report_id}")

        report = MarketAnalysisReport.model_validate(rows[0]["report_json"])
        updated = report.model_copy(update={"confidence_score": new_confidence})

        self._request(
            "PATCH",
            _TABLE,
            params={"id": f"eq.{report_id}"},
            json={
                "confidence_score": new_confidence,
                "report_json": updated.model_dump(mode="json"),
                "evaluated_at": evaluated_at.isoformat(),
                "outcome": outcome,
            },
        )

    def get_history_for_market(self, market_id: str) -> list[StoredReport]:
        rows = self._request(
            "GET",
            _TABLE,
            params={
                "market_id": f"eq.{market_id}",
                "select": "*",
                "order": "created_at.asc",
            },
        ).json()
        return [self._to_stored(row) for row in rows]

    @staticmethod
    def _to_stored(row: dict) -> StoredReport:
        return StoredReport(
            id=row["id"],
            market_id=row["market_id"],
            market_slug=row["market_slug"],
            # jsonb comes back as a parsed object, unlike SQLite's text column.
            report=MarketAnalysisReport.model_validate(row["report_json"]),
            price_at_report=row["price_at_report"],
            created_at=_parse_ts(row["created_at"]),
            evaluated_at=_parse_ts(row["evaluated_at"])
            if row["evaluated_at"]
            else None,
            outcome=row["outcome"],
        )


def _parse_ts(value: str) -> datetime:
    """Parse a Postgres timestamptz.

    Postgres renders UTC offsets as '+00:00' but older servers may emit a bare
    '+00'; fromisoformat rejects the latter, so it is normalised first.
    """
    if value.endswith("+00"):
        value = value[:-3] + "+00:00"
    return datetime.fromisoformat(value)
