"""B.11 — explanation calibration.

compute_calibration is pure over (stated_confidence, outcome) pairs, so every
case here is testable without a store or a live evaluation cycle.
"""

from src.evaluation.calibration import compute_calibration


def _pairs(n: int, confidence: float = 0.8, outcome: str = "CONFIRMED"):
    return [(confidence, outcome)] * n


class TestGate:
    def test_below_threshold_is_insufficient(self):
        result = compute_calibration(_pairs(299), minimum_for_display=300)
        assert result.sufficient is False
        assert result.bins == []

    def test_exactly_at_threshold_is_not_sufficient(self):
        # n > 300, strictly — the threshold itself is not "enough".
        result = compute_calibration(_pairs(300), minimum_for_display=300)
        assert result.sufficient is False
        assert result.bins == []

    def test_one_past_threshold_is_sufficient(self):
        result = compute_calibration(_pairs(301), minimum_for_display=300)
        assert result.sufficient is True
        assert result.bins != []

    def test_total_scored_is_reported_even_when_insufficient(self):
        # So the UI can show "127 of 300 collected", not just "not yet".
        result = compute_calibration(_pairs(127), minimum_for_display=300)
        assert result.total_scored == 127

    def test_minimum_for_display_is_echoed_back(self):
        result = compute_calibration(_pairs(5), minimum_for_display=300)
        assert result.minimum_for_display == 300

    def test_empty_input_is_insufficient_not_an_error(self):
        result = compute_calibration([], minimum_for_display=300)
        assert result.sufficient is False
        assert result.total_scored == 0
        assert result.bins == []


class TestBinning:
    def test_groups_into_tenths(self):
        scored = _pairs(200, confidence=0.72) + _pairs(200, confidence=0.85)
        result = compute_calibration(scored, minimum_for_display=300)
        edges = {(b.lower, b.upper) for b in result.bins}
        assert (0.7, 0.8) in edges
        assert (0.8, 0.9) in edges

    def test_a_stated_confidence_of_exactly_one_clamps_into_the_last_bin(self):
        scored = _pairs(400, confidence=1.0)
        result = compute_calibration(scored, minimum_for_display=300)
        assert len(result.bins) == 1
        assert (result.bins[0].lower, result.bins[0].upper) == (0.9, 1.0)

    def test_bin_mean_confidence_is_the_average_of_its_members(self):
        scored = [(0.70, "CONFIRMED")] * 200 + [(0.79, "CONFIRMED")] * 200
        result = compute_calibration(scored, minimum_for_display=300)
        bin_70 = next(b for b in result.bins if (b.lower, b.upper) == (0.7, 0.8))
        assert bin_70.stated_confidence == (0.70 + 0.79) / 2

    def test_sample_size_per_bin(self):
        scored = _pairs(150, confidence=0.6) + _pairs(151, confidence=0.65)
        result = compute_calibration(scored, minimum_for_display=300)
        bin_60 = next(b for b in result.bins if (b.lower, b.upper) == (0.6, 0.7))
        assert bin_60.sample_size == 301


class TestObservedAccuracy:
    def test_all_confirmed_is_full_accuracy(self):
        scored = _pairs(400, outcome="CONFIRMED")
        result = compute_calibration(scored, minimum_for_display=300)
        assert result.bins[0].observed_accuracy == 1.0

    def test_all_reversed_is_zero_accuracy(self):
        scored = _pairs(400, outcome="REVERSED")
        result = compute_calibration(scored, minimum_for_display=300)
        assert result.bins[0].observed_accuracy == 0.0

    def test_mixed_outcomes_give_a_fraction(self):
        scored = _pairs(300, outcome="CONFIRMED") + _pairs(100, outcome="REVERSED")
        result = compute_calibration(scored, minimum_for_display=300)
        assert result.bins[0].observed_accuracy == 300 / 400
