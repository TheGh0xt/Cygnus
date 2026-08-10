import json
import pathlib
import tempfile

from src.api.app import create_app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _spec():
    with tempfile.TemporaryDirectory() as tmp:
        return create_app(db_path=f"{tmp}/t.db").openapi()


def test_committed_openapi_matches_the_app():
    """The contract file is what the UI repo generates its client from.

    If this fails, run: python scripts/export_openapi.py
    """
    committed = json.loads((REPO_ROOT / "openapi.json").read_text())
    assert committed == _spec()


def test_openapi_documents_every_v1_route():
    spec = _spec()
    for path in (
        "/v1/health",
        "/v1/ready",
        "/v1/analyses",
        "/v1/analyses/{analysis_id}",
        "/v1/analyses/{analysis_id}/events",
    ):
        assert path in spec["paths"]
