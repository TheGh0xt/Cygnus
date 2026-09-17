"""Drives the ADK pipeline and translates its events into stage events.

ADK reports progress by event author (the agent that produced it). The API
exposes stable public stage names instead, so renaming an internal agent
does not break the UI contract.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from google.genai import types

from .errors import ErrorType
from .registry import AnalysisRegistry, StageEvent
from .usage import TokenUsage, add_event_usage

logger = logging.getLogger("cygnus.api.pipeline")

APP_NAME = "pmie"

STAGE_BY_AUTHOR = {
    "analysis_event_retrieval": "event_retrieval",
    "analysis_signal_retrieval": "signal_retrieval",
    "analysis_news_retrieval": "news_retrieval",
    "market_analyst_agent": "analysis",
}

# Authors whose raw tool output has_no_usable_market_data inspects. Retrieval
# only — the analyst and news stages never touch Sagittarius.
RETRIEVAL_AUTHORS = {"analysis_event_retrieval", "analysis_signal_retrieval"}


def stage_for_author(author: str) -> str | None:
    return STAGE_BY_AUTHOR.get(author)


class InsufficientMarketDataError(RuntimeError):
    """Retrieval finished but left nothing to reason over (F15).

    Raised instead of letting the pipeline continue into news retrieval and
    the analyst. A schema-valid "I had no data" report is not an exception —
    the analyst prompt's own UNKNOWN_ANOMALY/empty-market_id instructions
    make sure of that — so without this, the run finishes with
    outcome="completed" and silently spends a user's monthly credit on a
    report that explains nothing.
    """

    error_type = ErrorType.SAGITTARIUS_UNAVAILABLE


def _tool_result_payloads(event) -> list[dict]:
    """Every JSON object inside one event's raw MCP tool results.

    Reads get_function_responses(), not the retrieval agents' output_key
    text: a model that ignores "return the tool result exactly as received"
    would otherwise defeat this check the same way the analyst ignoring its
    own prompt produced F15 in the first place.

    Sagittarius wraps every tool result as TextContent —
    {"content": [{"type": "text", "text": "<json>"}]}, never
    structuredContent (internal/interface/mcp/tools/*.go in Sagittarius) — so
    the payload is one json.loads away from the response dict, not sitting
    at its top level.
    """
    get_responses = getattr(event, "get_function_responses", None)
    if get_responses is None:
        return []

    payloads: list[dict] = []
    for func_response in get_responses():
        response = getattr(func_response, "response", None)
        if not isinstance(response, dict):
            continue
        for item in response.get("content") or []:
            text = item.get("text") if isinstance(item, dict) else None
            if not text:
                continue
            try:
                payloads.append(json.loads(text))
            except (TypeError, ValueError):
                continue
    return payloads


def has_no_usable_market_data(events: list) -> bool:
    """Whether Sagittarius gave the retrieval stages nothing to reason over.

    Two shapes, both mapped to ErrorType.SAGITTARIUS_UNAVAILABLE by the
    caller: no market anywhere in retrieval output carries a condition_id
    (Sagittarius unreachable or erroring), or every market that does carry
    one reports probability 0.0 everywhere (a snapshot fetch that came back
    empty rather than erroring).

    Volume is deliberately excluded: a real, quiet market has a real
    probability and zero volume, and that is a fact worth reporting, not an
    outage. Gating on it would fix one fabrication by shipping another.
    """
    markets: list[dict] = []
    for event in events:
        for payload in _tool_result_payloads(event):
            markets.extend(payload.get("markets") or [])

    markets_with_id = [m for m in markets if isinstance(m, dict) and m.get("condition_id")]
    if not markets_with_id:
        return True

    probabilities = [m["probability"] for m in markets_with_id if "probability" in m]
    return bool(probabilities) and all(p == 0.0 for p in probabilities)


def build_runner(db_path: str):
    """Construct a Runner backed by a durable session service.

    SqliteSessionService rather than DatabaseSessionService: the latter
    requires the `google-adk[db]` sqlalchemy extra this project does not
    otherwise need.
    """
    from google.adk.runners import Runner
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    from ..agents.orchestrator import market_analysis_pipeline

    return Runner(
        app_name=APP_NAME,
        agent=market_analysis_pipeline,
        session_service=SqliteSessionService(db_path=db_path),
    )


REPORT_KEY = "market_analysis_report"
NEWS_KEY = "news_context_output"


def _citation_coverage_violations(report: dict, news_context: str | None) -> list[str]:
    """Rules the analyst's cited_sources output should satisfy, checked
    deterministically after the fact.

    B.12's prompt instructs the analyst to populate cited_sources with a
    provenance tier and a verification tag for every citation that bears on
    its stated cause. That is a prompt instruction, not something
    output_schema can enforce — a model that ignores it still produces a
    schema-valid report with cited_sources == [], and nothing would ever
    notice. This closes that gap the same way every other silent-failure
    precedent in this codebase gets closed: log it loudly, don't hide it
    behind a passing run.

    Returns the broken rules' descriptions, or [] if none. Never raises, and
    the caller never fails the analysis over it — a citation coverage gap is
    a quality signal to watch in production, not a broken run to reject.
    """
    violations: list[str] = []
    cited_sources = report.get("cited_sources") or []

    has_real_news = bool(news_context) and news_context.strip() != "NO_RELEVANT_NEWS"
    if has_real_news and not cited_sources:
        violations.append(
            "news_context_output had real items but cited_sources is empty"
        )

    if report.get("primary_causal_driver") == "EXTERNAL_NEWS" and not any(
        source.get("verification") == "SUPPORTS" for source in cited_sources
    ):
        violations.append(
            "primary_causal_driver is EXTERNAL_NEWS but no cited_sources "
            "entry has verification SUPPORTS"
        )

    return violations


def _state_delta(event) -> dict:
    """State an agent wrote on this event.

    ADK surfaces an agent's `output_key` write as `actions.state_delta`. The
    Session returned by `create_session` is a snapshot taken *before* the run,
    so reading its `.state` afterwards never sees the analyst's output —
    accumulating deltas is the only way to observe it while streaming.
    """
    actions = getattr(event, "actions", None)
    delta = getattr(actions, "state_delta", None) if actions else None
    return delta if isinstance(delta, dict) else {}


class AnalysisPipeline:
    def __init__(
        self,
        registry: AnalysisRegistry,
        runner,
        user_id: str = "pmie",
        accounts=None,
        persistence=None,
    ):
        self._registry = registry
        self._runner = runner
        self._user_id = user_id
        self._accounts = accounts
        self._persistence = persistence

    async def run(
        self,
        analysis_id: str,
        query: str,
        slug: str,
        profile_id: str | None = None,
    ) -> None:
        self._registry.mark_running(analysis_id)
        seen: set[str] = set()
        started = time.monotonic()
        outcome = "failed"
        try:
            session = await self._runner.session_service.create_session(
                app_name=APP_NAME,
                user_id=self._user_id,
                session_id=analysis_id,
                # Kept for the evaluation worker's benefit and for anyone
                # inspecting a session; persistence no longer reads it.
                state={"event_slug": slug},
            )
            message = types.Content(role="user", parts=[types.Part(text=query)])
            state: dict = {}
            usage = TokenUsage()
            retrieval_events: list = []

            async for event in self._runner.run_async(
                user_id=self._user_id,
                session_id=session.id,
                new_message=message,
            ):
                stage = stage_for_author(getattr(event, "author", ""))
                if stage and stage not in seen:
                    seen.add(stage)
                    self._registry.publish(
                        analysis_id, StageEvent("stage_started", stage, {})
                    )
                state.update(_state_delta(event))
                usage = add_event_usage(usage, event)

                if getattr(event, "author", None) in RETRIEVAL_AUTHORS:
                    retrieval_events.append(event)

                if event.is_final_response() and stage:
                    self._registry.publish(
                        analysis_id, StageEvent("stage_completed", stage, {})
                    )
                    # Checked right after retrieval, before news retrieval or
                    # the analyst ever run: the entire pipeline is one nested
                    # async generator, so stopping this consumer loop here
                    # means those later stages are never started, not merely
                    # discarded (F15). No quota spent, no report persisted.
                    if stage == "signal_retrieval" and has_no_usable_market_data(
                        retrieval_events
                    ):
                        raise InsufficientMarketDataError(
                            "Sagittarius returned no usable market data for "
                            "this event."
                        )

            final = state.get(REPORT_KEY)
            if final is None:
                # Deltas can be missed if a stage wrote state without emitting
                # a delta we saw; re-read the persisted session before failing.
                final = await self._report_from_session(session.id)
            if final is None:
                raise RuntimeError("pipeline produced no report")

            # An LLM cannot know the current time (F16) — production saw a
            # report stamped two years in the past. Set it here, once, so the
            # registry, the persisted row, and the SSE `report` event all
            # agree on the same correct value. This runs after the analyst's
            # output_schema has already validated that *a* timestamp exists;
            # only the value is being corrected.
            final["timestamp"] = datetime.now(UTC).isoformat()

            for violation in _citation_coverage_violations(final, state.get(NEWS_KEY)):
                logger.warning(
                    "analysis %s citation coverage: %s", analysis_id, violation
                )

            self._registry.mark_completed(analysis_id, final)
            outcome = "completed"

            # Persist here, where the report demonstrably exists.
            #
            # This used to live in the analyst's after_agent_callback, reading
            # market_analysis_report out of CallbackContext.state. That state
            # is session state plus the callback's *own* (empty) delta, so it
            # only contains the analyst's output_key write once ADK has
            # committed that event to the session — and in a real run it has
            # not yet. The callback saw nothing, returned quietly, and every
            # completed analysis was discarded while reporting success.
            #
            # The pipeline accumulates state deltas from the events themselves,
            # so by this line the report is in hand. Persisting here removes
            # the dependency on ADK's internal ordering entirely.
            self._persist(analysis_id, final, slug)

            self._registry.publish(analysis_id, StageEvent("report", None, final))
        except Exception as exc:
            logger.exception("analysis %s failed", analysis_id)
            error_type = getattr(exc, "error_type", None)
            self._registry.mark_failed(
                analysis_id,
                str(exc),
                error_type=error_type.value if error_type else None,
            )
            self._registry.publish(
                analysis_id,
                StageEvent(
                    "error",
                    None,
                    {
                        "detail": str(exc),
                        "error_type": error_type.value if error_type else None,
                    },
                ),
            )
        finally:
            # Always terminate the stream, success or failure, or an SSE
            # client waits forever.
            self._registry.close(analysis_id)
            duration_ms = int((time.monotonic() - started) * 1000)

            # Logged unconditionally, including for generated analyses that
            # have no profile. Those are exactly the runs whose cost needs
            # watching — they are the ones nobody is sitting in front of.
            logger.info(
                "analysis %s %s in %dms (%s tokens)",
                analysis_id,
                outcome,
                duration_ms,
                usage.total_tokens or "unmeasured",
            )

            if self._accounts is not None and profile_id:
                # Recorded for every attempt. Only 'completed' rows count
                # toward an allowance — a failed analysis must never consume
                # a credit, which is a stated term at the payment boundary.
                self._accounts.record_usage(
                    profile_id=profile_id,
                    analysis_id=analysis_id,
                    outcome=outcome,
                    event_slug=slug or None,
                    duration_ms=duration_ms,
                    tokens=usage,
                )

    def _persist(self, analysis_id: str, report: dict, slug: str) -> None:
        """Store a completed report with the price observed right now.

        Never raises into the run. The user has already waited a minute and a
        half for this report; failing their request would lose the analysis as
        well as the row. But the failure is logged at CRITICAL, because the
        observed price cannot be reconstructed afterwards and without it the
        report can never be scored.
        """
        if self._persistence is None:
            return
        try:
            report_id = self._persistence.save(report, slug)
            if report_id is not None:
                self._registry.set_report_id(analysis_id, report_id)
        except Exception:
            logger.critical(
                "REPORT LOST — analysis for %s completed but could not be "
                "persisted. This data cannot be recreated; the evaluation "
                "engine will never score it.",
                slug or "<unknown slug>",
                exc_info=True,
            )

    async def _report_from_session(self, session_id: str) -> dict | None:
        """Re-read the persisted session state as a fallback."""
        try:
            session = await self._runner.session_service.get_session(
                app_name=APP_NAME, user_id=self._user_id, session_id=session_id
            )
        except Exception:
            logger.exception("could not re-read session %s", session_id)
            return None
        state = getattr(session, "state", None) or {}
        return state.get(REPORT_KEY)
