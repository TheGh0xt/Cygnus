"""Layer 5 — explanation calibration (ROADMAP B.11).

Answers "when PMIE says 0.7, is it right about 70% of the time?" — not a
forecast of market outcomes, which PMIE never makes. Every scored report is
binned by what the analyst originally *stated* its confidence was, then
checked against whether the 48h evaluation confirmed or reversed it.

Binning must use the stated (pre-evaluation) confidence, not the current
confidence_score column, which the evaluation worker overwrites in place as
part of the self-correction loop. Binning on the adjusted value would be
circular: a report that came due to be adjusted upward has, by construction,
moved toward its own bin's "correct" side. See
memory/store.py:StoredReport.stated_confidence.
"""

from __future__ import annotations

from dataclasses import dataclass

# The published curve needs enough reports per bin to mean anything; a
# handful of reports drawing a "curve" is misleading, not informative. Below
# this, the API returns total_scored (so a "127 of 300" progress state is
# possible) but never bins.
MINIMUM_FOR_DISPLAY = 300

_BIN_WIDTH = 0.1
_BIN_COUNT = 10


@dataclass(frozen=True)
class CalibrationBinResult:
    lower: float
    upper: float
    stated_confidence: float
    observed_accuracy: float
    sample_size: int


@dataclass(frozen=True)
class CalibrationResult:
    bins: list[CalibrationBinResult]
    total_scored: int
    minimum_for_display: int
    sufficient: bool


def compute_calibration(
    scored: list[tuple[float, str]],
    minimum_for_display: int = MINIMUM_FOR_DISPLAY,
) -> CalibrationResult:
    """Bin (stated_confidence, outcome) pairs into a reliability curve.

    `scored` is deliberately primitives, not StoredReport: this stays a pure
    function of the two numbers a calibration curve actually needs, testable
    without a store. `outcome` is "CONFIRMED" or "REVERSED", as recorded by
    the evaluation worker's canonical (48h) checkpoint.

    Gated behind `n > minimum_for_display` — strictly more, so the threshold
    itself is never treated as "enough". Below the gate, `bins` is always
    empty: a curve is never returned half-formed, only the count so far.
    """
    total = len(scored)
    sufficient = total > minimum_for_display

    if not sufficient:
        return CalibrationResult(
            bins=[],
            total_scored=total,
            minimum_for_display=minimum_for_display,
            sufficient=False,
        )

    buckets: dict[int, list[tuple[float, str]]] = {}
    for stated_confidence, outcome in scored:
        # Tenths-wide bins, [0.0-0.1) .. [0.9-1.0]. A stated confidence of
        # exactly 1.0 clamps into the last bin rather than spilling into an
        # eleventh, empty one.
        index = min(int(stated_confidence * _BIN_COUNT), _BIN_COUNT - 1)
        buckets.setdefault(index, []).append((stated_confidence, outcome))

    bins = [_bin_result(index, buckets[index]) for index in sorted(buckets)]

    return CalibrationResult(
        bins=bins,
        total_scored=total,
        minimum_for_display=minimum_for_display,
        sufficient=True,
    )


def _bin_result(index: int, group: list[tuple[float, str]]) -> CalibrationBinResult:
    confirmed = sum(1 for _, outcome in group if outcome == "CONFIRMED")
    return CalibrationBinResult(
        lower=round(index * _BIN_WIDTH, 2),
        upper=round((index + 1) * _BIN_WIDTH, 2),
        stated_confidence=sum(c for c, _ in group) / len(group),
        observed_accuracy=confirmed / len(group),
        sample_size=len(group),
    )
