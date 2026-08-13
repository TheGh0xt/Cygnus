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

    Postgres whenever Supabase is configured, SQLite otherwise. Chosen by
    configuration rather than a flag so a deployed instance cannot
    accidentally run on a disk that disappears on restart — the failure mode
    there is silent, and only discovered when the accuracy record turns out to
    be empty.
    """
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        from .postgres_store import PostgresMemoryStore

        logger.info("memory store: postgres")
        return PostgresMemoryStore()

    logger.info("memory store: sqlite at %s", db_path)
    return SqliteMemoryStore(db_path)


__all__ = ["SqliteMemoryStore", "StoredReport", "build_memory_store"]
