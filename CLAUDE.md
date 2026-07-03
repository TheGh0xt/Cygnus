# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Cygnus is **Layer 4 (Reasoning Agent)** of the Prediction Market Intelligence Engine (PMIE) — a multi-layer system that explains *why* prices move on Polymarket prediction markets. Cygnus is built on Google's Agent Development Kit (ADK) and orchestrates specialist agents powered by Gemini.

The full 5-layer architecture:
- **Layer 1 — Sagittarius** (separate repo): Go/Rust MCP server exposing Polymarket data tools
- **Layer 2 — Signal Engine** (separate repo): deterministic anomaly detection, no LLM
- **Layer 3 — Memory Layer** (planned): vector/KV store for token optimization
- **Layer 4 — Cygnus** (this repo): ADK-based reasoning agent orchestration
- **Layer 5 — Evaluation Engine** (planned): T+48h accuracy backtesting cron worker

## Development Commands

```bash
# Activate the virtualenv (Python 3.14)
source .venv/bin/activate

# Run the agent in the ADK developer web UI
adk web

# Run the agent interactively in the terminal
adk run Cygnus

# Run with a local .env file loaded
adk run --env_file .env Cygnus
```

The `.adk/session.db` file stores conversation state for the dev UI — it is gitignored.

## Agent Architecture

### Entry Point

`agent.py` at the repo root exposes `root_agent = orchestrator`, which ADK discovers automatically because `__init__.py` imports it. This is the required ADK package convention.

### Agent Hierarchy (Sequential, not graph — see TODO in orchestrator.py)

```
orchestrator  (polymarket_orchestrator)
├── market_event_agent    — retrieves a single Polymarket event via Sagittarius MCP tools
└── formatter_agent       — formats structured output for readability only
```

Each agent is defined in `src/agents/` and its system prompt lives in the corresponding file under `src/prompts/`.

### Agent Design Rules (enforced in prompts)

- The orchestrator never performs analysis or summarizes data itself — it only routes.
- The event agent retrieves one event and returns the raw tool result; it never speculates or analyzes.
- The formatter presents data only; it never changes numeric values or infers missing fields.

### Sagittarius MCP Connection

`market_event_agent` connects to the Sagittarius MCP server via StreamableHTTP. The URL is read from the `SAGITTARIUS_MCP_URL` environment variable (default: `http://localhost:8080/mcp`). Sagittarius must be running before invoking the event agent.

Available MCP tools (exposed by Sagittarius):
- `get_event_by_id` — fetch by numeric Polymarket event ID
- `get_event_by_slug` — fetch by slug or extracted from a Polymarket URL

## Key Constraints

- The output contract for the full reasoning pipeline is a `MarketAnalysisReport` JSON schema (defined in `docs/docs_AGENT_SPEC.md`). Enforce it when implementing the reasoning output stage.
- Raw trade/order book data must **never** be sent to the LLM directly — it must be aggregated first (Memory Layer responsibility, Layer 3).
- The `master` branch is protected; all changes go through PRs via the `pre-push` hook in `scripts/hooks/pre-push`.
- The `docs/` directory is gitignored except for `docs_AGENT_SPEC.md`.

## Environment Variables

| Variable | Description |
|---|---|
| `SAGITTARIUS_MCP_URL` | HTTP URL of the Sagittarius MCP server (default: `http://localhost:8080/mcp`) |
| `GOOGLE_API_KEY` | Required by Google ADK for Gemini model access |

## Commit Conventions
- Never add "Co-Authored-By" lines or AI attribution to Git commits.
- Do not include Claude metadata in PR descriptions.
