"""Drives the ADK pipeline and translates its events into stage events.

ADK reports progress by event author (the agent that produced it). The API
exposes stable public stage names instead, so renaming an internal agent
does not break the UI contract.
"""

from __future__ import annotations

import logging

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


def _report_from_event(event, session_state: dict | None) -> dict | None:
    """Pull the analyst's structured output.

    Prefer session state (`market_analysis_report`, written by the analyst's
    output_key) and fall back to an event-carried report, which is what test
    fakes provide.
    """
    if session_state and session_state.get("market_analysis_report"):
        return session_state["market_analysis_report"]
    return getattr(event, "report", None)


class AnalysisPipeline:
    def __init__(self, registry: AnalysisRegistry, runner, user_id: str = "pmie"):
        self._registry = registry
        self._runner = runner
        self._user_id = user_id

    async def run(self, analysis_id: str, query: str, slug: str) -> None:
        self._registry.mark_running(analysis_id)
        seen: set[str] = set()
        try:
            session = await self._runner.session_service.create_session(
                app_name=APP_NAME,
                user_id=self._user_id,
                session_id=analysis_id,
                # The persistence callback reads this back out of state.
                state={"event_slug": slug},
            )
            message = types.Content(role="user", parts=[types.Part(text=query)])
            final = None

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
                if event.is_final_response():
                    if stage:
                        self._registry.publish(
                            analysis_id, StageEvent("stage_completed", stage, {})
                        )
                    final = (
                        _report_from_event(event, getattr(session, "state", None))
                        or final
                    )

            if final is None:
                raise RuntimeError("pipeline produced no report")

            self._registry.mark_completed(analysis_id, final)
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
