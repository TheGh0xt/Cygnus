"""Layer 3 memory store.

Two interchangeable implementations behind one contract:

- ``SqliteMemoryStore`` — zero infrastructure, used for local development and
  tests.
- ``PostgresMemoryStore`` — used by any deployed instance, because a hosted
  container's filesystem is ephemeral and this data cannot be recreated.

``build_memory_store`` picks between them, so callers never decide.
"""

from __future__ import annotations

import logging
import os

from .store import SqliteMemoryStore, StoredReport

logger = logging.getLogger("cygnus.memory")


def build_memory_store(db_path: str = "pmie_memory.db"):
    """Return the right store for this environment.

    Postgres whenever Supabase is configured, SQLite otherwise — except in
    production (PMIE_ENVIRONMENT=production), where the SQLite fallback is
    refused outright. A deployed container's filesystem is ephemeral, and
    each stored row carries the market price observed at report time, which
    cannot be reconstructed after the fact. Falling back silently there is
    exactly the shape of the 17-day silent `record_usage` failure this task
    exists to prevent, so a missing or unreachable Postgres in production
    raises here and fails startup instead of degrading.

    Outside production, behaviour is unchanged: Postgres when configured,
    SQLite otherwise, with no connectivity probe — local dev must not pay for
    a network round trip, or fail outright, over a misconfigured `.env`.
    """
    from ..api.config import is_production, supabase_secret_key, supabase_url

    postgres_configured = bool(supabase_url() and supabase_secret_key())

    if is_production():
        from .postgres_store import MemoryStoreError, PostgresMemoryStore

        if not postgres_configured:
            raise MemoryStoreError(
                "PMIE_ENVIRONMENT=production requires a configured Postgres "
                "memory store (SUPABASE_URL + a secret key); refusing to "
                "fall back to SQLite, whose data does not survive a "
                "container restart."
            )
        store = PostgresMemoryStore()
        # Configured is not the same as reachable — prove it before serving
        # a single request on it.
        store.ping()
        logger.info("memory store: postgres (production)")
        return store

    if postgres_configured:
        from .postgres_store import PostgresMemoryStore

        logger.info("memory store: postgres")
        return PostgresMemoryStore()

    logger.info("memory store: sqlite at %s", db_path)
    return SqliteMemoryStore(db_path)


__all__ = ["SqliteMemoryStore", "StoredReport", "build_memory_store"]
