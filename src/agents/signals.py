import os

from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from ..prompts.signals import SYSTEM_PROMPT

market_signal_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="market_signal_agent",
    description="Retrieves deterministic market signals (whale activity, orderbook skew, volume spikes) from the Sagittarius Signal Engine.",
    instruction=SYSTEM_PROMPT,
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
            ),
            tool_filter=["get_whale_activity", "get_market_snapshot"],
        ),
    ],
    output_key="market_signals_output",
)
