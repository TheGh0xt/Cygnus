"""Drives the ADK pipeline and translates its events into stage events.

ADK reports progress by event author (the agent that produced it). The API
exposes stable public stage names instead, so renaming an internal agent
does not break the UI contract.
"""

from __future__ import annotations

import logging
import time

from google.genai import types

from .registry import AnalysisRegistry, StageEvent

logger = logging.getLogger("cygnus.api.pipeline")

APP_NAME = "pmie"

STAGE_BY_AUTHOR = {
    "analysis_event_retrieval": "event_retrieval",
    "analysis_signal_retrieval": "signal_retrieval",
    "analysis_news_retrieval": "news_retrieval",
    "market_analyst_agent": "analysis",
}


def stage_for_author(author: str) -> str | None:
    return STAGE_BY_AUTHOR.get(author)


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

                if event.is_final_response() and stage:
                    self._registry.publish(
                        analysis_id, StageEvent("stage_completed", stage, {})
                    )

            final = state.get(REPORT_KEY)
            if final is None:
                # Deltas can be missed if a stage wrote state without emitting
                # a delta we saw; re-read the persisted session before failing.
                final = await self._report_from_session(session.id)
            if final is None:
                raise RuntimeError("pipeline produced no report")

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
            self._persist(final, slug)

            self._registry.publish(analysis_id, StageEvent("report", None, final))
        except Exception as exc:
            logger.exception("analysis %s failed", analysis_id)
            self._registry.mark_failed(analysis_id, str(exc))
            self._registry.publish(
                analysis_id, StageEvent("error", None, {"detail": str(exc)})
            )
        finally:
            # Always terminate the stream, success or failure, or an SSE
            # client waits forever.
            self._registry.close(analysis_id)
            if self._accounts is not None and profile_id:
                # Recorded for every attempt. Only 'completed' rows count
                # toward an allowance — a failed analysis must never consume
                # a credit, which is a stated term at the payment boundary.
                self._accounts.record_usage(
                    profile_id=profile_id,
                    analysis_id=analysis_id,
                    outcome=outcome,
                    event_slug=slug or None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

    def _persist(self, report: dict, slug: str) -> None:
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
            self._persistence.save(report, slug)
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
