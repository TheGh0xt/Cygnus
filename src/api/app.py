from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from ..config import sagittarius_url, warm_sagittarius
from ..evaluation.worker import SagittariusPriceFetcher
from ..memory import build_memory_store
from .accounts import Accounts
from .auth import JwksCache
from .errors import PmieError, problem_response
from .evaluation_routes import router as evaluation_router
from .logging import configure_logging, new_request_id, request_id_var
from .persistence import ReportPersistence
from .pipeline import AnalysisPipeline, build_runner
from .ratelimit import RateLimiter
from .registry import AnalysisRegistry
from .routes import router
from .selfcheck import run_startup_checks


def create_app(db_path: str = "pmie_memory.db") -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Confirm this process can actually persist a report.

        Runs at startup rather than on first use so a misconfiguration shows
        up in the deploy log, instead of being discovered weeks later when the
        accuracy record turns out to be empty.

        Never fatal. Refusing to boot would take the whole API down over a
        store problem, and analyses are still worth serving while it is fixed
        — the check exists to make the failure impossible to miss, not to
        decide the outcome.
        """
        if app.state.accounts.configured:
            app.state.write_access = run_startup_checks(app.state.accounts)
        # Wake Sagittarius now so the first real analysis is not the request
        # that pays for its cold start — that is what produced a report built
        # entirely from news, with no market data in it.
        warm_sagittarius()
        yield

    app = FastAPI(
        lifespan=lifespan,
        title="PMIE API",
        version="1.0.0",
        description="Causal explanations for Polymarket price moves.",
    )

    # Postgres when Supabase is configured, SQLite otherwise. A deployed
    # instance must not keep this on a container filesystem that is wiped
    # on restart — the observed price in each row cannot be recreated.
    store = build_memory_store(db_path)
    fetcher = SagittariusPriceFetcher(sagittarius_url())
    # Persistence runs in the pipeline, not as an ADK after-agent callback:
    # that callback cannot see the analyst's output_key write, because
    # CallbackContext.state is session state plus the callback's own empty
    # delta and ADK has not committed the analyst's event yet.
    persistence = ReportPersistence(store, fetcher)

    # Held on app state so the scheduled evaluation endpoint can reuse the
    # same store and price fetcher rather than constructing its own.
    app.state.memory_store = store
    app.state.price_fetcher = fetcher

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

    # Protects the model budget rather than rationing a scarce resource:
    # a per-user cap stops one account running away with it, a global cap
    # stops many well-behaved accounts exhausting it collectively. Both are
    # configurable, and 0 disables a limit.
    app.state.limiter = RateLimiter(
        per_user=int(os.getenv("PMIE_RATE_LIMIT_PER_USER", "10")),
        per_user_window=float(os.getenv("PMIE_RATE_LIMIT_WINDOW_SECONDS", "3600")),
        global_limit=int(os.getenv("PMIE_RATE_LIMIT_GLOBAL", "60")),
        global_window=float(os.getenv("PMIE_RATE_LIMIT_WINDOW_SECONDS", "3600")),
    )

    registry = AnalysisRegistry()
    app.state.registry = registry
    app.state.tasks = set()
    app.state.pipeline = AnalysisPipeline(
        registry,
        build_runner(f"{db_path}.sessions"),
        accounts=accounts,
        persistence=persistence,
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

    app.state.write_access = None
    app.include_router(router)
    app.include_router(evaluation_router)
    return app
