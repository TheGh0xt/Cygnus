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
    import importlib
    import pathlib
    import sys

    parent = str(pathlib.Path(__file__).resolve().parents[2])
    sys.path.insert(0, parent)
    try:
        pkg = importlib.import_module("Cygnus.agent")
        assert pkg.root_agent.name == "polymarket_orchestrator"
    finally:
        sys.path.remove(parent)


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
    assert "market_analyst_agent" in [a.name for a in orchestrator.sub_agents]
