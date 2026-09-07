"""Turning Sagittarius's get_moving_markets reply into candidates.

The parsing is tested directly rather than through a live MCP session: the
network shim is exercised by end-to-end verification, but the decode is where
a schema change would silently produce an empty feed, so it gets real tests.
"""

import json

import pytest

from src.generation.discovery import DiscoveryError, parse_moving_markets


def _tool_result(payload, is_error: bool = False):
    from mcp.types import CallToolResult, TextContent

    text = payload if isinstance(payload, str) else json.dumps(payload)
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=is_error
    )


class TestParsing:
    def test_reads_markets_into_candidates(self):
        result = _tool_result(
            {
                "categories": ["Politics"],
                "markets": [
                    {
                        "slug": "who-will-bernie-endorse",
                        "title": "Who will Bernie endorse?",
                        "category": "Politics",
                        "probability": 0.275,
                        "change_24h": 0.336,
                        "volume_24h": 1000.0,
                        "end_date": "2026-11-04T00:00:00Z",
                    }
                ],
            }
        )

        candidates = parse_moving_markets(result)

        assert len(candidates) == 1
        assert candidates[0].market_slug == "who-will-bernie-endorse"
        assert candidates[0].category == "Politics"
        assert candidates[0].change_24h == 0.336

    def test_empty_market_list_is_not_an_error(self):
        # A quiet period is a legitimate answer, distinct from a broken tool.
        assert parse_moving_markets(_tool_result({"markets": []})) == []

    def test_a_market_missing_its_slug_is_dropped_not_fatal(self):
        # One malformed row must not cost the whole cycle — the same rule the
        # Gamma timestamp bug taught on the Go side.
        result = _tool_result(
            {
                "markets": [
                    {"title": "no slug here", "probability": 0.4, "change_24h": 0.1},
                    {
                        "slug": "good-one",
                        "title": "fine",
                        "category": "Crypto",
                        "probability": 0.4,
                        "change_24h": 0.1,
                    },
                ]
            }
        )

        candidates = parse_moving_markets(result)

        assert [c.market_slug for c in candidates] == ["good-one"]


class TestFailuresAreLoud:
    """The price fetcher swallowed every failure into None and cost five days
    of misdiagnosis. This client raises a typed error instead."""

    def test_tool_error_raises(self):
        with pytest.raises(DiscoveryError):
            parse_moving_markets(_tool_result({"markets": []}, is_error=True))

    def test_non_json_reply_raises(self):
        with pytest.raises(DiscoveryError):
            parse_moving_markets(_tool_result("this is not json"))

    def test_missing_markets_key_raises(self):
        # Distinct from an empty list: the shape is wrong, which means the
        # tool contract changed and somebody needs to know.
        with pytest.raises(DiscoveryError):
            parse_moving_markets(_tool_result({"categories": ["Politics"]}))

    def test_empty_content_raises(self):
        from mcp.types import CallToolResult

        with pytest.raises(DiscoveryError):
            parse_moving_markets(CallToolResult(content=[], isError=False))
