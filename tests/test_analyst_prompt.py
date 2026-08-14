"""The analyst prompt's rules about market_id.

A production report was stored keyed on "world-cup-winner moving" — a
fragment of the user's question. Two causes, both now closed:

1. Sagittarius sent markets with no identifier at all, so the agent had
   nothing else to use (fixed in Sagittarius: markets now carry condition_id).
2. This prompt explicitly permitted it, saying to use "the market's question
   or condition_id".

A report keyed on question text cannot be grouped with its market or scored,
so it silently corrupts the accuracy record.
"""

import re

from src.prompts.analyst import SYSTEM_PROMPT

# The prompt is hard-wrapped, so assertions normalise whitespace rather than
# forcing the prose to fit a test's idea of line length.
FLAT = re.sub(r"\s+", " ", SYSTEM_PROMPT).lower()


class TestMarketIdRules:
    def test_requires_condition_id(self):
        assert "condition_id" in SYSTEM_PROMPT

    def test_no_longer_offers_the_question_as_an_alternative(self):
        # The exact phrasing that caused the bug.
        assert "question or condition_id" not in SYSTEM_PROMPT

    def test_forbids_using_question_or_slug_as_the_identifier(self):
        assert "never use the market's question" in FLAT

    def test_prefers_an_empty_identifier_to_an_invented_one(self):
        # An invented id looks valid and corrupts data silently; an empty one
        # is visibly missing.
        assert "invented identifier is worse than an absent one" in FLAT

    def test_tells_the_analyst_what_to_do_without_market_data(self):
        assert "UNKNOWN_ANOMALY" in SYSTEM_PROMPT


class TestExistingGuaranteesSurvive:
    def test_confidence_is_still_capped(self):
        assert "Never exceed 0.9" in SYSTEM_PROMPT

    def test_summary_length_rule_is_intact(self):
        assert "500 characters" in SYSTEM_PROMPT
