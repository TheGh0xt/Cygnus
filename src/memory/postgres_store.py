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
from datetime import UTC, datetime, timedelta

import httpx

from ..api.config import supabase_secret_key, supabase_url
from ..schemas.report import MarketAnalysisReport
from .store import Checkpoint, RecentAnalysis, StoredReport

logger = logging.getLogger("cygnus.memory.postgres")

_TABLE = "/analysis_reports"
_CHECKPOINTS = "/report_evaluations"


class MemoryStoreError(Exception):
    """Any failure talking to the report store."""


class PostgresMemoryStore:
    backend = "postgres"

    # Safely under PostgREST's common default db-max-rows of 1000 — a page
    # request equal to or above the server's own cap would come back
    # truncated at the cap, which this code would then misread as "last
    # page" and stop one page early.
    _PAGE_SIZE = 500

    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or supabase_url()).rstrip("/")
        self._service_key = service_key or supabase_secret_key()
        if not (self._base_url and self._service_key):
            raise MemoryStoreError(
                "PostgresMemoryStore needs SUPABASE_URL and a secret key "
                "(SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY)"
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

    def ping(self) -> None:
        """Prove the store is actually reachable, not just configured.

        A URL and key can be set and still point at nothing — a typo'd
        project ref, a paused project, a network partition. Cheapest possible
        real round trip: one row, no filtering. `_request` already raises
        MemoryStoreError on a connection failure or a non-2xx response, so
        the caller (build_memory_store, in production) just needs to let that
        propagate instead of catching it.
        """
        self._request("GET", _TABLE, params={"select": "id", "limit": 1})

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
                # Fixed here, never touched again — see StoredReport.stated_confidence.
                "stated_confidence_score": report.confidence_score,
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

    def recent_analyses(self, since: datetime) -> list[RecentAnalysis]:
        """Every analysis written since `since`, oldest first.

        Selects only the two columns the generation cycle actually needs.
        `select=*` here would pull every stored report's jsonb across the wire
        on every cycle to answer a question about slugs and timestamps.
        """
        rows = self._request(
            "GET",
            _TABLE,
            params={
                "created_at": f"gte.{since.isoformat()}",
                "select": "market_slug,created_at",
                "order": "created_at.asc",
            },
        ).json()
        return [
            RecentAnalysis(
                market_slug=row["market_slug"],
                created_at=_parse_ts(row["created_at"]),
            )
            for row in rows
        ]

    def get_recorded_horizons(self, report_id: int) -> set[int]:
        rows = self._request(
            "GET",
            _CHECKPOINTS,
            params={"report_id": f"eq.{report_id}", "select": "horizon_hours"},
        ).json()
        return {row["horizon_hours"] for row in rows}

    def get_checkpoints(self, report_id: int) -> list[Checkpoint]:
        rows = self._request(
            "GET",
            _CHECKPOINTS,
            params={
                "report_id": f"eq.{report_id}",
                "select": "*",
                "order": "horizon_hours.asc",
            },
        ).json()
        return [
            Checkpoint(
                horizon_hours=row["horizon_hours"],
                observed_price=row["observed_price"],
                outcome=row["outcome"],
                is_canonical=bool(row["is_canonical"]),
                evaluated_at=_parse_ts(row["evaluated_at"]),
            )
            for row in rows
        ]

    def record_checkpoint(
        self,
        report_id: int,
        horizon_hours: int,
        observed_price: float,
        outcome: str,
        evaluated_at: datetime,
        new_confidence: float | None = None,
    ) -> None:
        """Record one checkpoint, adjusting confidence only if canonical.

        `new_confidence` is passed only for the canonical horizon. Applying
        the confidence matrix at every horizon would move scores four times as
        far as it was designed to and let a wobbling report whipsaw itself.
        """
        is_canonical = new_confidence is not None

        self._request(
            "POST",
            _CHECKPOINTS,
            # The unique constraint on (report_id, horizon_hours) makes the
            # cycle idempotent; ignoring the duplicate keeps a re-run harmless.
            headers={"prefer": "resolution=ignore-duplicates"},
            json={
                "report_id": report_id,
                "horizon_hours": horizon_hours,
                "observed_price": observed_price,
                "outcome": outcome,
                "is_canonical": is_canonical,
                "evaluated_at": evaluated_at.isoformat(),
            },
        )

        if not is_canonical:
            return

        rows = self._request(
            "GET",
            _TABLE,
            params={"id": f"eq.{report_id}", "select": "report_json", "limit": 1},
        ).json()
        if not rows:
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

    def get_reports_awaiting_any_horizon(self, now: datetime) -> list[StoredReport]:
        """Reports with at least one horizon left to evaluate.

        A report leaves this set once its canonical checkpoint lands, which is
        what evaluated_at marks.
        """
        rows = self._request(
            "GET",
            _TABLE,
            params={
                "evaluated_at": "is.null",
                "created_at": f"lte.{now.isoformat()}",
                "select": "*",
                "order": "created_at.asc",
            },
        ).json()
        return [self._to_stored(row) for row in rows]

    def get_scored_reports(self) -> list[StoredReport]:
        """Every report with a canonical (48h) outcome recorded.

        The read behind B.11's calibration curve: only a report that has
        actually been scored against what the market did 48 hours later
        belongs in a reliability calculation.
        """
        rows = self._request(
            "GET",
            _TABLE,
            params={"outcome": "not.is.null", "select": "*"},
        ).json()
        return [self._to_stored(row) for row in rows]

    def get_scored_confidence_outcomes(self) -> list[tuple[float, str]]:
        """Narrow, paginated read behind the public calibration curve.

        `get_scored_reports` pulls the full jsonb `report_json` for every
        scored row — fine for the evaluation worker, a cost/DoS surface on an
        unauthenticated route that only ever needs two numbers per row.
        Paginates with limit/offset because PostgREST caps a single response
        at `db-max-rows` (1000 by default on Supabase); past that many scored
        reports, an unpaginated read would silently truncate `total_scored`
        and the curve rather than erroring.
        """
        pairs: list[tuple[float, str]] = []
        offset = 0
        while True:
            rows = self._request(
                "GET",
                _TABLE,
                params={
                    "outcome": "not.is.null",
                    "select": "stated_confidence_score,confidence_score,outcome",
                    "order": "id.asc",
                    "limit": self._PAGE_SIZE,
                    "offset": offset,
                },
            ).json()
            pairs.extend(
                (
                    row["stated_confidence_score"]
                    if row.get("stated_confidence_score") is not None
                    else row["confidence_score"],
                    row["outcome"],
                )
                for row in rows
            )
            if len(rows) < self._PAGE_SIZE:
                break
            offset += self._PAGE_SIZE
        return pairs

    @staticmethod
    def _to_stored(row: dict) -> StoredReport:
        # .get(), not row[...]: a deployment that hasn't yet run the
        # stated_confidence_score migration must not 500 on every read.
        stated = row.get("stated_confidence_score")
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
            stated_confidence=stated if stated is not None else row["confidence_score"],
        )


def _parse_ts(value: str) -> datetime:
    """Parse a Postgres timestamptz.

    Postgres renders UTC offsets as '+00:00' but older servers may emit a bare
    '+00'; fromisoformat rejects the latter, so it is normalised first.
    """
    if value.endswith("+00"):
        value = value[:-3] + "+00:00"
    return datetime.fromisoformat(value)
