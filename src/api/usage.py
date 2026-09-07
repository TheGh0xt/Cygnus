"""Token accounting — ROADMAP 5.6.

Until this existed there was no token measurement anywhere in Cygnus. That
made two later decisions guesses: the generation budget (8 analyses a day)
was set against an unknown unit cost, and Phase 6 pricing would have been set
against that guess. 6.1 requires margin to be positive on the heaviest
realistic user, which cannot be established without knowing what one analysis
costs.

Measured in the shared pipeline rather than in the generation cycle, so
human-initiated analyses are counted too — the heaviest user is a person, not
the cron.

Pure, so it is testable without ADK or a model call.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    response_tokens: int = 0
    # Kept separately rather than derived, because Gemini's own total includes
    # tokens the two component counts do not — cached content, for one — and
    # the billed figure is the one worth recording.
    total_tokens: int = 0

    def __bool__(self) -> bool:
        """False when nothing was measured.

        Lets the caller omit the token columns rather than writing zeroes,
        which in a cost record would be indistinguishable from an analysis
        that genuinely cost nothing.
        """
        return bool(self.prompt_tokens or self.response_tokens or self.total_tokens)

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "response_tokens": self.response_tokens,
            "total_tokens": self.total_tokens,
        }


def add_event_usage(usage: TokenUsage, event: object) -> TokenUsage:
    """Fold one ADK event's usage into a running total.

    A run is four sequential stages, so an analysis costs the sum over every
    event rather than whatever the last one reports. Most events carry no
    usage at all and are ignored.
    """
    metadata = getattr(event, "usage_metadata", None)
    if metadata is None:
        return usage

    prompt = _count(metadata, "prompt_token_count")
    response = _count(metadata, "candidates_token_count")
    total = _count(metadata, "total_token_count")

    # Fall back to the components when Gemini omits the total: an honest
    # approximation beats reporting zero for a run that cost real money.
    if total == 0:
        total = prompt + response

    return TokenUsage(
        prompt_tokens=usage.prompt_tokens + prompt,
        response_tokens=usage.response_tokens + response,
        total_tokens=usage.total_tokens + total,
    )


def _count(metadata: object, field: str) -> int:
    """Read one count, treating an absent or null field as zero.

    Gemini omits fields rather than sending zeroes, so None is the normal
    case, not an error.
    """
    value = getattr(metadata, field, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value
