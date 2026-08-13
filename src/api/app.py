from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request

from ..agents.analyst import attach_persistence
from ..evaluation.worker import SagittariusPriceFetcher
from ..memory import build_memory_store
from .accounts import Accounts
from .auth import JwksCache
from .errors import PmieError, problem_response
from .logging import configure_logging, new_request_id, request_id_var
from .pipeline import AnalysisPipeline, build_runner
from .registry import AnalysisRegistry
from .routes import router


def create_app(db_path: str = "pmie_memory.db") -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="PMIE API",
        version="1.0.0",
        description="Causal explanations for Polymarket price moves.",
    )

    # Postgres when Supabase is configured, SQLite otherwise. A deployed
    # instance must not keep this on a container filesystem that is wiped
    # on restart — the observed price in each row cannot be recreated.
    store = build_memory_store(db_path)
    fetcher = SagittariusPriceFetcher(
        os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
    )
    attach_persistence(store, fetcher)

    accounts = Accounts()
    app.state.accounts = accounts
    app.state.jwks = JwksCache()

    # Auth is on by default. Disabling it is a startup-time decision captured
    # here, so a stray environment variable cannot switch off authentication
    # part-way through a running process.
    app.state.auth_disabled = os.getenv("PMIE_AUTH_DISABLED") == "1"
    if app.state.auth_disabled:
        logging.getLogger("cygnus.api").warning(
            "authentication is DISABLED; every request runs as a fixed local user"
        )

    registry = AnalysisRegistry()
    app.state.registry = registry
    app.state.tasks = set()
    app.state.pipeline = AnalysisPipeline(
        registry, build_runner(f"{db_path}.sessions"), accounts=accounts
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        token = request_id_var.set(new_request_id())
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id_var.get()
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(PmieError)
    async def handle_pmie_error(request: Request, exc: PmieError):
        return problem_response(exc, instance=str(request.url.path))

    app.include_router(router)
    return app
