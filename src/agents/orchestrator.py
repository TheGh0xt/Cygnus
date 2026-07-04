# TODO: change to a graph workflow agent as complexity increases
# orchestrate using a sequential agent for now.

from google.adk.agents.llm_agent import LlmAgent

from ..prompts.orchestrator import SYSTEM_PROMPT
from . import events, formatter, signals

orchestrator = LlmAgent(
    model="gemini-2.5-flash",
    name="polymarket_orchestrator",
    description="Routes requests to the correct specialist agent.",
    instruction=SYSTEM_PROMPT,
    sub_agents=[
        events.market_event_agent,
        signals.market_signal_agent,
        formatter.formatter_agent,
    ],
)
