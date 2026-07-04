import os

from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from ..prompts.events import SYSTEM_PROMPT


def make_market_event_agent(name: str = "market_event_agent") -> LlmAgent:
    """Builds an event-retrieval agent. ADK requires unique agent names and a
    single parent per instance, so the analysis pipeline creates its own copy
    under a different name."""
    return LlmAgent(
        model="gemini-2.5-flash",
        name=name,
        description="You are the Event Retrieval Agent for the Polymarket Intelligence Engine.",
        instruction=SYSTEM_PROMPT,
        tools=[
            McpToolset(
                connection_params=StreamableHTTPConnectionParams(
                    url=os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
                ),
                tool_filter=["get_event_by_slug", "get_event_by_id"],
            ),
        ],
        output_key="event_details_output",
    )


market_event_agent = make_market_event_agent()
