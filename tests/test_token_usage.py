"""Counting what an analysis actually cost — ROADMAP 5.6.

Before this there was no token accounting anywhere in Cygnus: zero
occurrences of usage_metadata in src/. So the generation budget (8/day) was a
guess against an unmeasured unit price, and Phase 6 pricing would have been a
guess on top of that guess.

The accumulator is pure so it can be tested without ADK or a model call.
"""

from types import SimpleNamespace

from src.api.usage import TokenUsage, add_event_usage


def _event(prompt=None, candidates=None, total=None, missing=False):
    if missing:
        return SimpleNamespace(author="agent")
    return SimpleNamespace(
        author="agent",
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            total_token_count=total,
        ),
    )


class TestAccumulating:
    def test_starts_at_zero(self):
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.total_tokens == 0

    def test_sums_across_events(self):
        # A run is four sequential stages, so the cost of one analysis is the
        # sum over every event, not the last one's figures.
        usage = TokenUsage()
        usage = add_event_usage(usage, _event(prompt=100, candidates=20, total=120))
        usage = add_event_usage(usage, _event(prompt=300, candidates=50, total=350))

        assert usage.prompt_tokens == 400
        assert usage.response_tokens == 70
        assert usage.total_tokens == 470

    def test_an_event_without_usage_metadata_is_ignored(self):
        # Most ADK events carry no usage at all.
        usage = add_event_usage(TokenUsage(), _event(missing=True))
        assert usage.total_tokens == 0

    def test_none_counts_are_treated_as_zero(self):
        # Gemini omits fields rather than sending zeroes.
        usage = add_event_usage(TokenUsage(), _event(prompt=None, candidates=5, total=5))
        assert usage.prompt_tokens == 0
        assert usage.response_tokens == 5

    def test_totals_are_derived_when_absent(self):
        # If total_token_count is missing, prompt+response is still the honest
        # answer — better than reporting zero for a run that cost real money.
        usage = add_event_usage(TokenUsage(), _event(prompt=100, candidates=25, total=None))
        assert usage.total_tokens == 125

    def test_recorded_totals_win_over_derived_ones(self):
        # Gemini's own total includes tokens the two component fields do not,
        # such as cached content, so trust it when present.
        usage = add_event_usage(TokenUsage(), _event(prompt=100, candidates=25, total=400))
        assert usage.total_tokens == 400


class TestReporting:
    def test_exposes_a_dict_for_the_usage_row(self):
        usage = add_event_usage(TokenUsage(), _event(prompt=10, candidates=2, total=12))
        assert usage.as_dict() == {
            "prompt_tokens": 10,
            "response_tokens": 2,
            "total_tokens": 12,
        }

    def test_is_falsy_when_nothing_was_measured(self):
        # Lets the caller omit token columns entirely rather than writing
        # zeroes that would look like a free analysis in the cost record.
        assert not TokenUsage()
        assert add_event_usage(TokenUsage(), _event(prompt=1, candidates=0, total=1))


class TestUsageRowPayload:
    """What actually reaches the analysis_usage table."""

    def _accounts(self, monkeypatch):
        from src.api.accounts import Accounts

        accounts = Accounts(base_url="https://example.test", service_key="k")
        sent = {}

        def fake_request(method, path, **kwargs):
            sent["method"] = method
            sent["path"] = path
            sent["json"] = kwargs.get("json")

            class R:
                @staticmethod
                def json():
                    return []

            return R()

        monkeypatch.setattr(accounts, "_request", fake_request)
        return accounts, sent

    def test_token_counts_are_written_when_measured(self, monkeypatch):
        accounts, sent = self._accounts(monkeypatch)

        accounts.record_usage(
            profile_id="p1",
            analysis_id="a1",
            outcome="completed",
            tokens=TokenUsage(prompt_tokens=900, response_tokens=120, total_tokens=1020),
        )

        assert sent["json"]["total_tokens"] == 1020
        assert sent["json"]["prompt_tokens"] == 900

    def test_token_columns_are_omitted_when_nothing_was_measured(self, monkeypatch):
        # Writing zeroes would make an unmeasured run indistinguishable from a
        # free one and drag the average cost down.
        accounts, sent = self._accounts(monkeypatch)

        accounts.record_usage(profile_id="p1", analysis_id="a1", outcome="completed")

        assert "total_tokens" not in sent["json"]

    def test_a_rejected_write_never_reaches_the_caller(self, monkeypatch):
        # The token columns need a migration. Until it is applied PostgREST
        # rejects them, and that must cost a reporting row, not an analysis.
        from src.api.accounts import Accounts, AccountsError

        accounts = Accounts(base_url="https://example.test", service_key="k")

        def boom(*a, **kw):
            raise AccountsError("column total_tokens does not exist")

        monkeypatch.setattr(accounts, "_request", boom)

        accounts.record_usage(
            profile_id="p1",
            analysis_id="a1",
            outcome="completed",
            tokens=TokenUsage(1, 2, 3),
        )
