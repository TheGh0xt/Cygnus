import pytest

from src.api.registry import AnalysisRegistry, AnalysisStatus, StageEvent


def test_create_returns_pending_record_with_unique_id():
    reg = AnalysisRegistry()
    a = reg.create("why did X move?")
    b = reg.create("why did Y move?")
    assert a.analysis_id != b.analysis_id
    assert a.status is AnalysisStatus.PENDING
    assert a.query == "why did X move?"


def test_lifecycle_transitions():
    reg = AnalysisRegistry()
    rec = reg.create("q")
    reg.mark_running(rec.analysis_id)
    assert reg.get(rec.analysis_id).status is AnalysisStatus.RUNNING
    reg.mark_completed(rec.analysis_id, {"market_id": "0xabc"})
    got = reg.get(rec.analysis_id)
    assert got.status is AnalysisStatus.COMPLETED
    assert got.report == {"market_id": "0xabc"}


def test_failure_records_reason():
    reg = AnalysisRegistry()
    rec = reg.create("q")
    reg.mark_failed(rec.analysis_id, "sagittarius down")
    got = reg.get(rec.analysis_id)
    assert got.status is AnalysisStatus.FAILED
    assert got.error == "sagittarius down"


def test_get_unknown_id_returns_none():
    assert AnalysisRegistry().get("nope") is None


@pytest.mark.asyncio
async def test_publish_and_close_stream_events():
    reg = AnalysisRegistry()
    rec = reg.create("q")
    reg.publish(rec.analysis_id, StageEvent("stage_started", "analysis", {}))
    reg.close(rec.analysis_id)
    first = await rec.queue.get()
    assert first.event == "stage_started"
    assert await rec.queue.get() is None  # sentinel terminates the stream
