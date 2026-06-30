import os

from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from ..prompts.events import SYSTEM_PROMPT

market_event_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="market_event_agent",
    description="You are the Event Retrieval Agent for the Polymarket Intelligence Engine.",
    instruction=SYSTEM_PROMPT,
    # mode="task",
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
            ),
            # tool_filter=["get_event_by_slug"],
        ),
    ],
    output_key="event_details_output",
)
