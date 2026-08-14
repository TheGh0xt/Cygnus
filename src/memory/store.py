"""Layer 3 memory store — SQLite MVP.

Persists MarketAnalysisReports (plus the market price observed at report
time) so the Layer 5 evaluation worker can backtest them at T+48h, and so
future reasoning runs can retrieve historical explanations per market.

SQLite keeps the MVP infrastructure-free; the schema (schema.sql) is kept
portable for the planned pgvector-backed store. All writes to this store
happen in Cygnus — Sagittarius never touches it.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..schemas.report import MarketAnalysisReport

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class StoredReport:
    id: int
    market_id: str
    market_slug: str
    report: MarketAnalysisReport
    # None when the price fetch failed at report time; such reports are
    # persisted anyway and skipped by the evaluation cycle.
    price_at_report: float | None
    created_at: datetime
    evaluated_at: datetime | None
    outcome: str | None  # "CONFIRMED" | "REVERSED" | None


@dataclass
class Checkpoint:
    """One evaluation of a report at one horizon."""

    horizon_hours: int
    observed_price: float
    outcome: str  # "CONFIRMED" | "REVERSED"
    is_canonical: bool
    evaluated_at: datetime


class SqliteMemoryStore:
    def __init__(self, db_path: str | Path):
        # check_same_thread=False because the API opens this store once at
        # startup and then uses it from request handlers, which run on
        # different threads. Python's default guard rejects that outright,
        # even though SQLite itself serialises access fine.
        #
        # The lock is what makes that safe: it serialises our own use, so a
        # write from one request cannot interleave with a read from another.
        # Low volume, so contention is not a concern; correctness is.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA_PATH.read_text())
            self._conn.commit()

    def save_report(
        self,
        report: MarketAnalysisReport,
        market_slug: str,
        price_at_report: float | None,
        created_at: datetime | None = None,
    ) -> int:
        created = created_at or datetime.now(tz=UTC)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO analysis_reports
                    (market_id, market_slug, report_json, confidence_score,
                     price_at_report, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report.market_id,
                    market_slug,
                    report.model_dump_json(),
                    report.confidence_score,
                    price_at_report,
                    created.isoformat(),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_reports_due_for_evaluation(
        self, now: datetime, min_age_hours: int = 48
    ) -> list[StoredReport]:
        cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM analysis_reports
                WHERE evaluated_at IS NULL AND created_at <= ?
                ORDER BY created_at
                """,
                (cutoff,),
            ).fetchall()
        return [self._to_stored(r) for r in rows]

    def record_evaluation(
        self,
        report_id: int,
        new_confidence: float,
        outcome: str,
        evaluated_at: datetime,
    ) -> None:
        # Read and write under one lock: two concurrent cycles must not both
        # read the same report and each write a confidence adjustment.
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM analysis_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no stored report with id {report_id}")

            report = MarketAnalysisReport.model_validate_json(row["report_json"])
            updated = report.model_copy(update={"confidence_score": new_confidence})

            self._conn.execute(
                """
                UPDATE analysis_reports
                SET confidence_score = ?, report_json = ?, evaluated_at = ?,
                    outcome = ?
                WHERE id = ?
                """,
                (
                    new_confidence,
                    updated.model_dump_json(),
                    evaluated_at.isoformat(),
                    outcome,
                    report_id,
                ),
            )
            self._conn.commit()

    def get_history_for_market(self, market_id: str) -> list[StoredReport]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM analysis_reports
                WHERE market_id = ?
                ORDER BY created_at
                """,
                (market_id,),
            ).fetchall()
        return [self._to_stored(r) for r in rows]

    def get_recorded_horizons(self, report_id: int) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT horizon_hours FROM report_evaluations WHERE report_id = ?",
                (report_id,),
            ).fetchall()
        return {row["horizon_hours"] for row in rows}

    def get_checkpoints(self, report_id: int) -> list[Checkpoint]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM report_evaluations
                WHERE report_id = ?
                ORDER BY horizon_hours
                """,
                (report_id,),
            ).fetchall()
        return [
            Checkpoint(
                horizon_hours=row["horizon_hours"],
                observed_price=row["observed_price"],
                outcome=row["outcome"],
                is_canonical=bool(row["is_canonical"]),
                evaluated_at=datetime.fromisoformat(row["evaluated_at"]),
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
        """Record one checkpoint, and adjust confidence only if canonical.

        `new_confidence` is passed only for the canonical horizon. Earlier
        checkpoints are observations: applying the confidence matrix at every
        horizon would move scores four times as far as it was designed to, and
        let a report that wobbles whipsaw its own confidence.
        """
        is_canonical = new_confidence is not None
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO report_evaluations
                    (report_id, horizon_hours, observed_price, outcome,
                     is_canonical, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    horizon_hours,
                    observed_price,
                    outcome,
                    1 if is_canonical else 0,
                    evaluated_at.isoformat(),
                ),
            )

            if is_canonical:
                row = self._conn.execute(
                    "SELECT report_json FROM analysis_reports WHERE id = ?",
                    (report_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"no stored report with id {report_id}")

                report = MarketAnalysisReport.model_validate_json(row["report_json"])
                updated = report.model_copy(update={"confidence_score": new_confidence})
                self._conn.execute(
                    """
                    UPDATE analysis_reports
                    SET confidence_score = ?, report_json = ?, evaluated_at = ?,
                        outcome = ?
                    WHERE id = ?
                    """,
                    (
                        new_confidence,
                        updated.model_dump_json(),
                        evaluated_at.isoformat(),
                        outcome,
                        report_id,
                    ),
                )

            self._conn.commit()

    def get_reports_awaiting_any_horizon(self, now: datetime) -> list[StoredReport]:
        """Reports that still have at least one horizon left to evaluate.

        A report leaves this set once its canonical checkpoint is recorded,
        which is what `evaluated_at` marks.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM analysis_reports
                WHERE evaluated_at IS NULL AND created_at <= ?
                ORDER BY created_at
                """,
                (now.isoformat(),),
            ).fetchall()
        return [self._to_stored(r) for r in rows]

    @staticmethod
    def _to_stored(row: sqlite3.Row) -> StoredReport:
        return StoredReport(
            id=row["id"],
            market_id=row["market_id"],
            market_slug=row["market_slug"],
            report=MarketAnalysisReport.model_validate_json(row["report_json"]),
            price_at_report=row["price_at_report"],
            created_at=datetime.fromisoformat(row["created_at"]),
            evaluated_at=(
                datetime.fromisoformat(row["evaluated_at"])
                if row["evaluated_at"]
                else None
            ),
            outcome=row["outcome"],
        )
