"""Invariants of the agent tree that prompts and ADK discovery rely on."""

from src.agents.orchestrator import orchestrator


def test_orchestrator_identity():
    assert orchestrator.name == "polymarket_orchestrator"
    assert orchestrator.model == "gemini-2.5-flash"


def test_orchestrator_has_event_and_formatter_specialists():
    names = [a.name for a in orchestrator.sub_agents]
    assert "market_event_agent" in names
    assert "formatter_agent" in names


def test_root_agent_is_exposed_for_adk_discovery():
    """ADK discovers the agent by importing '<repo dir name>.agent' from the
    repo's parent directory — exactly what `adk run <dirname>` does.

    The repo directory isn't always named "Cygnus": a git worktree checks
    this same repo out under its own directory name (e.g.
    Cygnus/.claude/worktrees/reasoning), so the package name is derived from
    the actual directory rather than hardcoded.
    """
    import importlib
    import pathlib
    import sys

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    parent = str(repo_root.parent)
    package_name = repo_root.name
    sys.path.insert(0, parent)
    try:
        pkg = importlib.import_module(f"{package_name}.agent")
        assert pkg.root_agent.name == "polymarket_orchestrator"
    finally:
        sys.path.remove(parent)
        sys.modules.pop(package_name, None)
        sys.modules.pop(f"{package_name}.agent", None)


def test_signal_agent_registered():
    from src.agents.signals import market_signal_agent

    names = [a.name for a in orchestrator.sub_agents]
    assert "market_signal_agent" in names
    assert market_signal_agent.output_key == "market_signals_output"


def test_analyst_agent_enforces_output_contract():
    from src.agents.analyst import market_analyst_agent
    from src.schemas.report import MarketAnalysisReport

    assert market_analyst_agent.output_schema is MarketAnalysisReport
    assert market_analyst_agent.output_key == "market_analysis_report"
    assert not market_analyst_agent.tools


def test_analysis_pipeline_is_sequential_and_complete():
    """Causal analysis must run every stage deterministically: event
    retrieval, signal retrieval, news retrieval, then the schema-enforced
    analyst."""
    from src.agents.orchestrator import market_analysis_pipeline

    assert "market_analysis_pipeline" in [a.name for a in orchestrator.sub_agents]
    stages = [a.name for a in market_analysis_pipeline.sub_agents]
    assert stages == [
        "analysis_event_retrieval",
        "analysis_signal_retrieval",
        "analysis_news_retrieval",
        "market_analyst_agent",
    ]
    keys = [a.output_key for a in market_analysis_pipeline.sub_agents]
    assert keys == [
        "event_details_output",
        "market_signals_output",
        "news_context_output",
        "market_analysis_report",
    ]


def test_news_agent_registered_on_orchestrator():
    names = [a.name for a in orchestrator.sub_agents]
    assert "news_context_agent" in names


def test_analyst_prompt_injects_news_context():
    """The analyst must read the news digest via an optional placeholder —
    the '?' suffix keeps a missing key from raising outside the pipeline."""
    from src.prompts.analyst import SYSTEM_PROMPT

    assert "{news_context_output?}" in SYSTEM_PROMPT
    assert "NO_RELEVANT_NEWS" in SYSTEM_PROMPT


def test_analyst_agent_disallows_transfers():
    """output_schema forces Gemini JSON mode, which cannot coexist with the
    transfer_to_agent function declarations ADK attaches to sub-agents."""
    from src.agents.analyst import market_analyst_agent

    assert market_analyst_agent.disallow_transfer_to_parent
    assert market_analyst_agent.disallow_transfer_to_peers
