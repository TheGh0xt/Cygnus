from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from ..config import sagittarius_connection_params
from ..prompts.signals import SYSTEM_PROMPT


def make_market_signal_agent(name: str = "market_signal_agent") -> LlmAgent:
    """Builds a signal-retrieval agent. ADK requires unique agent names and a
    single parent per instance, so the analysis pipeline creates its own copy
    under a different name."""
    return LlmAgent(
        model="gemini-2.5-flash",
        name=name,
        description="Retrieves deterministic market signals (whale activity, orderbook skew, volume spikes) from the Sagittarius Signal Engine.",
        instruction=SYSTEM_PROMPT,
        tools=[
            McpToolset(
                connection_params=sagittarius_connection_params(),
                tool_filter=["get_whale_activity", "get_market_snapshot"],
            ),
        ],
        output_key="market_signals_output",
    )


market_signal_agent = make_market_signal_agent()
