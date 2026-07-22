"""Invariants of the news retrieval agent (Milestone 1: EXTERNAL_NEWS grounding)."""

from google.adk.tools.google_search_tool import GoogleSearchTool

from src.agents.news import make_news_context_agent, news_context_agent


def test_news_agent_identity():
    assert news_context_agent.name == "news_context_agent"
    assert news_context_agent.model == "gemini-2.5-flash"
    assert news_context_agent.output_key == "news_context_output"


def test_news_agent_uses_only_google_search():
    """google_search is a Gemini built-in tool and cannot be mixed with
    function tools (like the MCP toolsets) on one agent."""
    assert len(news_context_agent.tools) == 1
    assert isinstance(news_context_agent.tools[0], GoogleSearchTool)


def test_factory_builds_unique_instances():
    """ADK requires unique names and a single parent per agent instance, so
    the analysis pipeline must be able to mint its own copy."""
    copy = make_news_context_agent("analysis_news_retrieval")
    assert copy.name == "analysis_news_retrieval"
    assert copy is not news_context_agent
    assert copy.output_key == "news_context_output"


def test_news_prompt_guardrails():
    """Retrieve-only: impact analysis belongs to the analyst; the sentinel
    keeps 'no news' distinguishable from 'stage skipped' downstream."""
    from src.prompts.news import SYSTEM_PROMPT

    assert "NO_RELEVANT_NEWS" in SYSTEM_PROMPT
    assert "never" in SYSTEM_PROMPT.lower()
