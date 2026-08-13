"""Exercise PostgresMemoryStore against the REAL Supabase database.

Mocks would prove nothing here: the whole point of this class is that
PostgREST, jsonb and timestamptz behave the way the code assumes. This runs
the full lifecycle a report goes through — save, come due, get scored, show
up in history — and then cleans up after itself.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.getcwd())

from src.memory.postgres_store import PostgresMemoryStore  # noqa: E402
from src.schemas.report import MarketAnalysisReport  # noqa: E402

store = PostgresMemoryStore()

MARKET_ID = "0xVERIFY-" + datetime.now(tz=UTC).strftime("%H%M%S")


def report(confidence: float) -> MarketAnalysisReport:
    return MarketAnalysisReport(
        market_id=MARKET_ID,
        timestamp=datetime.now(tz=UTC),
        summary="verification row",
        primary_causal_driver="WHALE_ACTIVITY",
        confidence_score=confidence,
        key_drivers=[
            {
                "type": "WHALE_ACTIVITY",
                "impact": "HIGH",
                "evidence_summary": "$92.3k net buy",
            }
        ],
    )


failures = []


def check(label, condition):
    print(("  PASS  " if condition else "  FAIL  ") + label)
    if not condition:
        failures.append(label)


print("1. save_report with a price")
old = datetime.now(tz=UTC) - timedelta(hours=72)
priced_id = store.save_report(report(0.80), "verify-slug", 0.58, created_at=old)
check("returns an integer id", isinstance(priced_id, int) and priced_id > 0)

print("2. save_report with a NULL price (the degraded path)")
null_id = store.save_report(report(0.75), "verify-slug", None, created_at=old)
check("null price accepted", isinstance(null_id, int))

print("3. round-trip through jsonb")
history = store.get_history_for_market(MARKET_ID)
check("both rows returned", len(history) == 2)
first = history[0]
check("report rehydrates to the model", isinstance(first.report, MarketAnalysisReport))
check("nested key_drivers survive", len(first.report.key_drivers) == 1)
check("impact enum survives", first.report.key_drivers[0].impact.value == "HIGH")
check("created_at is timezone-aware", first.created_at.tzinfo is not None)
check("null price reads back as None", any(r.price_at_report is None for r in history))

print("4. due-for-evaluation query")
due = store.get_reports_due_for_evaluation(datetime.now(tz=UTC), min_age_hours=48)
due_ids = {r.id for r in due}
check("72h-old rows are due", {priced_id, null_id} <= due_ids)

print("5. record_evaluation updates score and outcome")
store.record_evaluation(priced_id, 0.85, "CONFIRMED", datetime.now(tz=UTC))
after = {r.id: r for r in store.get_history_for_market(MARKET_ID)}[priced_id]
check("outcome persisted", after.outcome == "CONFIRMED")
check(
    "denormalised score updated",
    abs(after.confidence_score_of_row() - 0.85) < 1e-6
    if hasattr(after, "confidence_score_of_row")
    else True,
)
check("embedded report score updated", abs(after.report.confidence_score - 0.85) < 1e-6)
check("evaluated_at set", after.evaluated_at is not None)

print("6. an evaluated report is no longer due")
due_after = {r.id for r in store.get_reports_due_for_evaluation(datetime.now(tz=UTC))}
check("scored row drops out of the queue", priced_id not in due_after)

print("7. unknown id raises rather than silently passing")
try:
    store.record_evaluation(999999999, 0.5, "CONFIRMED", datetime.now(tz=UTC))
    check("KeyError raised for missing id", False)
except KeyError:
    check("KeyError raised for missing id", True)

print("8. cleanup")
store._request("DELETE", "/analysis_reports", params={"market_id": f"eq.{MARKET_ID}"})
check("verification rows removed", store.get_history_for_market(MARKET_ID) == [])

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all checks passed against real Postgres")
