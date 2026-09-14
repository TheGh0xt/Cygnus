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


class TestClaimVerificationAndSourceTiering:
    """B.12: each cited_sources entry must be tagged with a provenance tier
    and a verification tag. Report v2's schema already carries these fields
    (frozen in Wave 0) — this is the reasoning step that actually populates
    them; per ADK's rules (output_schema forbids tools) it has to be prompt
    instructions over injected state, not a tool call."""

    def test_names_the_provenance_tiers(self):
        for tier in ("PRIMARY", "PARTIAL", "WEAK"):
            assert tier in SYSTEM_PROMPT

    def test_names_the_verification_tags(self):
        for tag in ("SUPPORTS", "CONTRADICTS", "UNSUPPORTED"):
            assert tag in SYSTEM_PROMPT

    def test_forbids_inventing_citation_fields(self):
        assert "never invent a title, publisher, url, or date" in FLAT

    def test_empty_news_means_empty_citations(self):
        assert "cited_sources must be empty" in SYSTEM_PROMPT

    def test_contradicting_evidence_is_wanted_not_hidden(self):
        # The whole point of tagging CONTRADICTS: an analyst tempted to
        # quietly omit evidence against its own conclusion must be told not
        # to, explicitly, or the confidence score stops meaning anything.
        assert "not a weakness in the report" in FLAT

    def test_external_news_driver_requires_a_supporting_citation(self):
        assert (
            "cited_sources must contain at least one entry with verification supports"
            in FLAT
        )

    def test_does_not_require_citing_every_retrieved_item(self):
        # cited_sources is an evidence ledger, not a transcript — irrelevant
        # retrieved items should be dropped, not force-tagged UNSUPPORTED.
        assert "not a transcript of everything retrieved" in FLAT
