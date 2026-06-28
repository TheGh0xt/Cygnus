import os

from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset

from src.prompts.events import SYSTEM_PROMPT

market_event_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="market_event_agent",
    description="You are the Event Retrieval Agent for the Polymarket Intelligence Engine.",
    instruction=SYSTEM_PROMPT,
    mode="task",
    tools=[
        MCPToolset(
            connection_params=os.getenv(
                "SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp"
            )
        ),
    ],
    output_key="event_details_output",
)
