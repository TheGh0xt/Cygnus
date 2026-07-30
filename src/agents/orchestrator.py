from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.sequential_agent import SequentialAgent

from ..prompts.orchestrator import SYSTEM_PROMPT
from . import analyst, events, formatter, news, signals

# Causal analysis is a fixed cognitive flow (AGENT_SPEC section 1), so it runs
# as a deterministic SequentialAgent rather than trusting LLM-driven transfers
# to visit every stage: retrieve event intelligence, retrieve deterministic
# signals, retrieve news context, then synthesize the schema-validated
# MarketAnalysisReport.
market_analysis_pipeline = SequentialAgent(
    name="market_analysis_pipeline",
    description=(
        "Full causal-analysis flow for one Polymarket event: retrieves event "
        "intelligence, retrieves deterministic signals (whales, skew, volume), "
        "retrieves recent cited news for the event's real-world subject, then "
        "produces a structured MarketAnalysisReport explaining WHY the "
        "market moved."
    ),
    sub_agents=[
        events.make_market_event_agent("analysis_event_retrieval"),
        signals.make_market_signal_agent("analysis_signal_retrieval"),
        news.make_news_context_agent("analysis_news_retrieval"),
        analyst.market_analyst_agent,
    ],
)

orchestrator = LlmAgent(
    model="gemini-2.5-flash",
    name="polymarket_orchestrator",
    description="Routes requests to the correct specialist agent.",
    instruction=SYSTEM_PROMPT,
    sub_agents=[
        events.market_event_agent,
        signals.market_signal_agent,
        news.news_context_agent,
        market_analysis_pipeline,
        formatter.formatter_agent,
    ],
)
