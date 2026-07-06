from google.adk.agents.llm_agent import LlmAgent

from ..prompts.analyst import SYSTEM_PROMPT
from ..schemas.report import MarketAnalysisReport

# NOTE: ADK forbids tools on agents with an output_schema — the analyst is a
# pure reasoning step over state produced by the event and signal agents.
# Transfers must also be disallowed: output_schema puts Gemini in JSON
# response mode, which cannot coexist with the transfer_to_agent function
# declarations ADK would otherwise attach to a sub-agent
# ("Function calling with a response mime type: 'application/json' is unsupported").
market_analyst_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="market_analyst_agent",
    description="Synthesizes a causal MarketAnalysisReport from retrieved event data and deterministic signals.",
    instruction=SYSTEM_PROMPT,
    output_schema=MarketAnalysisReport,
    output_key="market_analysis_report",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
