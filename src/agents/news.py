from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools import google_search

from ..prompts.news import SYSTEM_PROMPT


def make_news_context_agent(name: str = "news_context_agent") -> LlmAgent:
    """Builds a news-retrieval agent. ADK requires unique agent names and a
    single parent per instance, so the analysis pipeline creates its own copy
    under a different name.

    google_search is a Gemini built-in tool and must be this agent's ONLY
    tool — mixing it with function tools (e.g. the MCP toolsets) on one agent
    is rejected by the API. That constraint is why news retrieval is a
    dedicated stage instead of a tool on the event agent.
    """
    return LlmAgent(
        model="gemini-2.5-flash",
        name=name,
        description=(
            "Retrieves recent, cited real-world news relevant to a Polymarket "
            "event via Google Search grounding."
        ),
        instruction=SYSTEM_PROMPT,
        tools=[google_search],
        output_key="news_context_output",
    )


news_context_agent = make_news_context_agent()
