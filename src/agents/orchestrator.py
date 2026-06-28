# TODO: change to a graph workflow agent as complexity increases
# orchestrate using a sequential agent for now.

from google.adk.agents.llm_agent import LlmAgent

from src.agents import events, formatter
from src.prompts.orchestrator import SYSTEM_PROMPT

orchestrator = LlmAgent(
    model="gemini-2.5-flash",
    name="polymarket_orchestrator",
    description="Routes requests to the correct specialist agent.",
    instruction=SYSTEM_PROMPT,
    sub_agents=[
        events.market_event_agent,
        formatter.formatter_agent,
    ],
)
