# DOCUMENT 3: AI REASONING AGENT & EVALUATION TECHNICAL SPECIFICATION
**File Path:** `docs/AGENT_SPEC.md`  
**Audience:** AI Agent Architects, LLM Engineers, Evaluation Framework Contributors  
**Status:** Reference Implementation Specification  

---

## 1. Core Reasoning Flow & Cognitive Architecture
The Reasoning Agent operates as an isolated cognitive layer, consuming data abstractions provided by lower layers. It relies on deterministic signals to structure its thoughts rather than browsing raw logs manually.

```
                  ┌──────────────────────────────┐
                  │   Temporal Alert / Ingestion │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 1. State Ingestion & Parsing │ (Consumes Scored Signals +
                  └──────────────┬───────────────┘  Condensed Semantic Context)
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 2. Vector Context Synthesis  │ (Queries Memory Layer for
                  └──────────────┬───────────────┘  Historical Parallel Cases)
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 3. Hypothesis Formulation   │ (Cross-references External
                  └──────────────┬───────────────┘  News APIs if signals call for it)
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 4. Structured Output Mapping │ (Generates strict JSON schema
                  └──────────────┬───────────────┘  with internal confidence metrics)
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 5. Post-Evaluation Tracking  │ (Asynchronous Backtesting loop
                  └──────────────────────────────┘  adjusts accuracy parameters)
```

---

## 2. Rigid Output Contracts & Schemas
To guarantee that downstream services, front-end layers, and automated execution webhooks can process agent insights safely, the Reasoning Agent must enforce a strict, type-checked JSON output contract.

### Production Output Schema Blueprint
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "MarketAnalysisReport",
  "type": "object",
  "properties": {
    "market_id": { "type": "string" },
    "timestamp": { "type": "string", "format": "date-time" },
    "summary": { "type": "string", "maxLength": 500 },
    "primary_causal_driver": {
      "type": "string",
      "enum": ["WHALE_ACTIVITY", "VOLUME_SPIKE", "LIQUIDITY_CRUNCH", "EXTERNAL_NEWS", "UNKNOWN_ANOMALY"]
    },
    "confidence_score": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
    "key_drivers": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "type": { "type": "string" },
          "impact": { "type": "string", "enum": ["HIGH", "MEDIUM", "LOW"] },
          "evidence_summary": { "type": "string" }
        },
        "required": ["type", "impact", "evidence_summary"]
      }
    },
    "historical_context_match": {
      "type": "object",
      "properties": {
        "previous_market_id": { "type": "string" },
        "prior_explanation_accuracy": { "type": "number" }
      }
    }
  },
  "required": ["market_id", "timestamp", "summary", "primary_causal_driver", "confidence_score", "key_drivers"]
}
```

---

## 3. Token-Optimized Memory Strategy
To prevent compounding infrastructure bills and context window starvation, the agent does not ingest raw event text. 

* **The Transformation Protocol:** The Memory Layer sits between the raw data and the LLM. It translates hundreds of low-level lines into an aggregated state snapshot:
  ```text
  [INBOUND RAW CONTEXT (BANNED FROM LLM)]
  - 12:01:02: Wallet 0xAx... purchased 50,000 YES tokens for $0.43
  - 12:01:45: Wallet 0xAx... purchased 100,000 YES tokens for $0.48
  - 12:02:10: Wallet 0xBx... sold 10,000 YES tokens for $0.47
  ... (repeated 1000 times)
  
  [OUTBOUND CONDENSED SEMANTIC CONTEXT (SENT TO LLM)]
  {
    "largest_concentrated_trade": "$250,000",
    "buy_sell_ratio": "87:13",
    "volume_velocity_change": "+320%",
    "unique_trader_count": 4
  }
  ```

---

## 4. Evaluation Strategy & Closed-Loop Feedback Mechanics
This architecture handles a critical problem: tracking whether agent explanations hold up to reality over time.

### The Accuracy Feedback Mechanism:
1. **Instantiation:** When an anomaly is detected, the agent writes its explanation JSON to the database with an initial `confidence_score` (e.g., `0.85`), tracking the asset price ($0.58).
2. **Delayed Execution (T+48 Hours):** A cron-driven evaluation worker checks the market state 48 hours later.
3. **Deterministic Verification Matrix:**
   * If `primary_causal_driver` was flagged as `WHALE_ACTIVITY` with a `HIGH` impact trend, and the order book displays sustained position holding or further upward migration $ightarrow$ **Confidence Weight Incremented**.
   * If the market completely reversed within hours, implying the whale was simply rebalancing or performing a short-lived arbitrage play $ightarrow$ **Confidence Weight Decremented**.
4. **Self-Correction Loop:** The updated historical accuracy is injected back into Layer 3 (Memory), ensuring that the next time a similar signal pattern triggers, the agent adjusts its reasoning based on real historical outcomes.
