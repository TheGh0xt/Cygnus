"""In-process registry of running and completed analyses.

Deliberately in-memory: an analysis is a single request's work, and a
restart losing in-flight runs is acceptable. Completed *reports* are
durable — they go to the memory store (see api/persistence.py),
which is the record that matters.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum


class AnalysisStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class StageEvent:
    event: str
    stage: str | None
    data: dict


@dataclass
class AnalysisRecord:
    analysis_id: str
    query: str
    status: AnalysisStatus = AnalysisStatus.PENDING
    report: dict | None = None
    error: str | None = None
    # A stable slug (see api.errors.ErrorType) a client can switch on, mirroring
    # the synchronous PmieError path — plain None for a failure with no typed
    # cause, so old clients reading only `error` see no behavior change.
    error_type: str | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    # Who started this run. None for system-generated analyses (the
    # scheduled discovery cycle), which have no owner to check against.
    profile_id: str | None = None
    # The durable memory store's row id, set once persistence succeeds.
    # Distinct from analysis_id: this record is in-process and ephemeral,
    # while report_id is what a share token actually points at (see
    # sharing.py) — a link that must keep meaning the same thing even though
    # the in-memory record it was minted against will not survive a restart.
    report_id: int | None = None


class AnalysisRegistry:
    def __init__(self) -> None:
        self._records: dict[str, AnalysisRecord] = {}

    def create(self, query: str, profile_id: str | None = None) -> AnalysisRecord:
        analysis_id = uuid.uuid4().hex
        record = AnalysisRecord(
            analysis_id=analysis_id, query=query, profile_id=profile_id
        )
        self._records[analysis_id] = record
        return record

    def get(self, analysis_id: str) -> AnalysisRecord | None:
        return self._records.get(analysis_id)

    def mark_running(self, analysis_id: str) -> None:
        self._records[analysis_id].status = AnalysisStatus.RUNNING

    def mark_completed(self, analysis_id: str, report: dict) -> None:
        record = self._records[analysis_id]
        record.status = AnalysisStatus.COMPLETED
        record.report = report

    def mark_failed(
        self, analysis_id: str, error: str, error_type: str | None = None
    ) -> None:
        record = self._records[analysis_id]
        record.status = AnalysisStatus.FAILED
        record.error = error
        record.error_type = error_type

    def set_report_id(self, analysis_id: str, report_id: int) -> None:
        self._records[analysis_id].report_id = report_id

    def publish(self, analysis_id: str, event: StageEvent) -> None:
        self._records[analysis_id].queue.put_nowait(event)

    def close(self, analysis_id: str) -> None:
        # None is the stream sentinel: the SSE generator stops on it.
        self._records[analysis_id].queue.put_nowait(None)
