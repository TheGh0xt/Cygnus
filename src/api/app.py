from __future__ import annotations

import os

from fastapi import FastAPI, Request

from ..agents.analyst import attach_persistence
from ..evaluation.worker import SagittariusPriceFetcher
from ..memory.store import SqliteMemoryStore
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

    store = SqliteMemoryStore(db_path)
    fetcher = SagittariusPriceFetcher(
        os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
    )
    attach_persistence(store, fetcher)

    registry = AnalysisRegistry()
    app.state.registry = registry
    app.state.tasks = set()
    app.state.pipeline = AnalysisPipeline(registry, build_runner(f"{db_path}.sessions"))

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
