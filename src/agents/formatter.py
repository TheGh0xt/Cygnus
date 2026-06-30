from google.adk.agents.llm_agent import LlmAgent

from ..prompts.formatter import SYSTEM_PROMPT

formatter_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="formatter_agent",
    description="An expert response formatter for the Polymarket Intelligence Engine",
    instruction=SYSTEM_PROMPT,
    output_key="formatted_response",
)
