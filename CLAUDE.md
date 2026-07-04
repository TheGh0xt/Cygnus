# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Cygnus is **Layer 4 (Reasoning Agent)** of the Prediction Market Intelligence Engine (PMIE) — a multi-layer system that explains *why* prices move on Polymarket prediction markets. Cygnus is built on Google's Agent Development Kit (ADK) and orchestrates specialist agents powered by Gemini.

The 5 layers map onto exactly **two repos**, split by runtime and synchronous coupling — do not create new repos for Layers 2, 3, or 5:

- **Layer 1 — Sagittarius** (Sagittarius repo, Go): MCP server exposing Polymarket data tools
- **Layer 2 — Signal Engine** (Sagittarius repo, Go): deterministic anomaly detection, no LLM; in-process with Layer 1 (hot path — never behind an MCP hop), exposing scored signals as additional MCP tools
- **Layer 3 — Memory Layer** (this repo, planned): vector/KV store for token optimization; Layer 4 needs its semantic context on essentially every reasoning call, and their schemas evolve in lockstep
- **Layer 4 — Cygnus** (this repo): ADK-based reasoning agent orchestration
- **Layer 5 — Evaluation Engine** (this repo, planned): T+48h accuracy backtesting as a cron-style subpackage reading the Memory Layer's store; split into its own deployable only when it needs independent scaling or release cadence

The MCP connection to Sagittarius is the **only** cross-repo seam. All Memory Layer writes happen in this repo — Cygnus fetches scored signals over MCP and stores them itself; Sagittarius never writes to the memory store directly.

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

# Install dev dependencies and run tests (from the repo root)
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v

# Run the T+48h evaluation worker (needs Sagittarius running)
.venv/bin/python -m src.evaluation.worker --db pmie_memory.db
```

The `.adk/session.db` file stores conversation state for the dev UI — it is gitignored.

## Agent Architecture

### Entry Point

`agent.py` at the repo root exposes `root_agent = orchestrator`, which ADK discovers automatically because `__init__.py` imports it. This is the required ADK package convention.

### Agent Hierarchy (Sequential, not graph — see TODO in orchestrator.py)

```
orchestrator  (polymarket_orchestrator)
├── market_event_agent    — retrieves a single Polymarket event via Sagittarius MCP tools
├── market_signal_agent   — retrieves deterministic signals (whale activity, market snapshot) via Sagittarius
├── market_analyst_agent  — pure reasoning: synthesizes a MarketAnalysisReport from the two outputs above
└── formatter_agent       — formats structured output for readability only
```

Each agent is defined in `src/agents/` and its system prompt lives in the corresponding file under `src/prompts/`. The event and signal agents write to session state (`event_details_output`, `market_signals_output`); the analyst reads both via prompt placeholders and emits `market_analysis_report` validated against `src/schemas/report.py` (`output_schema=MarketAnalysisReport` — which is why it has no tools; ADK forbids tools with an output schema).

### Agent Design Rules (enforced in prompts)

- The orchestrator never performs analysis or summarizes data itself — it only routes.
- The event and signal agents retrieve data and return raw tool results; they never speculate or analyze.
- The analyst reasons ONLY over injected state, cites concrete numbers as evidence, and caps confidence at 0.9.
- The formatter presents data only; it never changes numeric values or infers missing fields.

### Layers 3 & 5 (in this repo)

- `src/memory/` — **Layer 3 MVP**: `SqliteMemoryStore` persists `MarketAnalysisReport`s with the price observed at report time (`schema.sql` is kept portable for the planned pgvector store; vector search deferred). All writes happen in Cygnus — Sagittarius never touches this store.
- `src/evaluation/` — **Layer 5**: `run_evaluation_cycle` backtests due reports (default T+48h) with the deterministic matrix in `evaluate_report`: price held/extended → CONFIRMED (+0.05 confidence, cap 1.0); reversed beyond a 0.02 tolerance → REVERSED (−0.10, floor 0.0). Unfetchable markets stay due for the next cycle. CLI: `python -m src.evaluation.worker --db <path>`.

### Sagittarius MCP Connection

`market_event_agent` connects to the Sagittarius MCP server via StreamableHTTP. The URL is read from the `SAGITTARIUS_MCP_URL` environment variable (default: `http://localhost:8080/mcp`). Sagittarius must be running before invoking the event agent.

Available MCP tools (exposed by Sagittarius):
- `get_event_by_id` — fetch by numeric Polymarket event ID (event agent)
- `get_event_by_slug` — fetch by slug or extracted from a Polymarket URL (event agent)
- `get_market_snapshot` — per-market probability, orderbook skew, volume-spike analysis, whale count (signal agent)
- `get_whale_activity` — whale-sized trades per market with buy/sell ratio (signal agent)

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
