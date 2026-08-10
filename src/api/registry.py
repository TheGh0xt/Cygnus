"""In-process registry of running and completed analyses.

Deliberately in-memory: an analysis is a single request's work, and a
restart losing in-flight runs is acceptable. Completed *reports* are
durable — they go to the SQLite memory store (see agents/callbacks.py),
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
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class AnalysisRegistry:
    def __init__(self) -> None:
        self._records: dict[str, AnalysisRecord] = {}

    def create(self, query: str) -> AnalysisRecord:
        analysis_id = uuid.uuid4().hex
        record = AnalysisRecord(analysis_id=analysis_id, query=query)
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

    def mark_failed(self, analysis_id: str, error: str) -> None:
        record = self._records[analysis_id]
        record.status = AnalysisStatus.FAILED
        record.error = error

    def publish(self, analysis_id: str, event: StageEvent) -> None:
        self._records[analysis_id].queue.put_nowait(event)

    def close(self, analysis_id: str) -> None:
        # None is the stream sentinel: the SSE generator stops on it.
        self._records[analysis_id].queue.put_nowait(None)
