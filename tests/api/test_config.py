"""Supabase configuration is read under both supported names.

Supabase's own connect panel hands out SUPABASE_SECRET_KEY. Copying that
block into a deployment is the obvious move, so a server configured that way
must work — otherwise a correctly-copied config produces an unconfigured
server, and the symptom is a 503 from an unrelated endpoint.
"""

import pytest

from src.api.accounts import Accounts
from src.api.config import (
    describe_supabase_config,
    supabase_secret_key,
    supabase_url,
)
from src.memory import build_memory_store
from src.memory.postgres_store import PostgresMemoryStore


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class TestSecretKeyNames:
    def test_reads_our_original_name(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_aaa")
        assert supabase_secret_key() == "sb_secret_aaa"

    def test_reads_supabases_own_name(self, monkeypatch):
        # The name Supabase's connect panel actually gives you.
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_bbb")
        assert supabase_secret_key() == "sb_secret_bbb"

    def test_our_name_wins_when_both_are_set(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_ours")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_theirs")
        assert supabase_secret_key() == "sb_secret_ours"

    def test_missing_returns_empty(self):
        assert supabase_secret_key() == ""

    def test_empty_value_does_not_shadow_the_other_name(self, monkeypatch):
        # An env var set to "" in a dashboard must not mask a real key.
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_real")
        assert supabase_secret_key() == "sb_secret_real"


class TestWhitespace:
    def test_key_is_stripped(self, monkeypatch):
        # Copy-paste from a dashboard routinely brings a trailing newline, and
        # a key with whitespace fails auth in a way that looks like a bad key.
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "  sb_secret_ccc\n")
        assert supabase_secret_key() == "sb_secret_ccc"

    def test_url_is_stripped_and_normalised(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", " https://x.supabase.co/\n")
        assert supabase_url() == "https://x.supabase.co"


class TestConsumersHonourBothNames:
    def test_accounts_is_configured_under_supabases_name(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_ddd")
        assert Accounts().configured is True

    def test_store_selection_honours_supabases_name(self, monkeypatch, tmp_path):
        # The important one: without this, a deployment configured from the
        # connect panel silently falls back to SQLite on an ephemeral disk.
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_eee")
        assert isinstance(
            build_memory_store(str(tmp_path / "unused.db")), PostgresMemoryStore
        )


class TestDiagnostics:
    def test_summary_never_exposes_the_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_supersecretvalue")
        summary = describe_supabase_config()
        assert "supersecretvalue" not in str(summary)
        assert summary["secret_key_set"] is True

    def test_summary_shows_enough_to_spot_the_wrong_key_type(self, monkeypatch):
        # Pasting the publishable key where the secret belongs is the mistake
        # worth catching at a glance.
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_publishable_wrongkey")
        assert describe_supabase_config()["secret_key_prefix"] == "sb_publisha"
