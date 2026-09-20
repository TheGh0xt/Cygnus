"""Static guard: every raw MCP client to Sagittarius carries auth.

B.3 wires the bearer token into three separate call sites — the ADK agent
toolsets (via StreamableHTTPConnectionParams' own `headers`), the evaluation
worker, and generation discovery — because nothing forces a fourth one to
remember. That's exactly how discovery's own client was nearly shipped
without it: found only by a second pass, not by any test.

This scans src/ for every `streamable_http_client(` call using the raw `mcp`
SDK (the two ADK-toolset call sites use StreamableHTTPConnectionParams
instead and are out of scope here) and fails if any of them doesn't pass
`http_client=` — the only way those calls carry a header on this SDK version.
It does not check the header's value is correct, only that the call site
didn't skip auth wiring entirely.
"""

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

_CALL = re.compile(r"streamable_http_client\(")
_LOOKAHEAD_CHARS = 200


def _find_unwired_calls() -> list[str]:
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text()
        for match in _CALL.finditer(text):
            window = text[match.end() : match.end() + _LOOKAHEAD_CHARS]
            if "http_client" not in window:
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(SRC_ROOT.parent)}:{line_no}")
    return offenders


def test_every_streamable_http_client_call_is_wired_for_auth():
    offenders = _find_unwired_calls()
    assert offenders == [], (
        "streamable_http_client(...) called without http_client= (so it "
        "can't carry MCP_BEARER_TOKEN) at: " + ", ".join(offenders)
    )


def test_every_sagittarius_client_defaults_to_the_configured_token(monkeypatch):
    """Constructing a client with only a URL must still carry auth.

    The static guard above passes `http_client=` and stops there — it says so
    itself. That was not enough: `SagittariusPriceFetcher` defaulted its
    headers to `{}`, `mcp_http_client({})` yields None, and the SDK then
    builds its own unauthenticated client. The `http_client=` argument was
    present the whole time, so the guard was green while
    `api/app.py`'s fetcher — the one the running API process uses — sent no
    token at all, and every stored report lost its observed price.

    This asserts the behaviour the guard only approximates, across every
    Sagittarius MCP client, so a fourth one cannot repeat it.
    """
    from src.evaluation.worker import SagittariusPriceFetcher
    from src.generation.discovery import SagittariusDiscovery

    monkeypatch.setenv("MCP_BEARER_TOKEN", "secret-token")
    expected = {"Authorization": "Bearer secret-token"}

    clients = [
        SagittariusPriceFetcher("http://localhost:8080/mcp"),
        SagittariusDiscovery("http://localhost:8080/mcp"),
    ]

    unauthenticated = [
        type(client).__name__ for client in clients if client._headers != expected
    ]
    assert unauthenticated == [], (
        "these Sagittarius MCP clients drop the bearer token when constructed "
        "with only a URL, which is how api/app.py constructs them: "
        + ", ".join(unauthenticated)
    )


def test_the_guard_actually_finds_something_when_it_should():
    # Proves the regex isn't vacuously matching nothing. If src/ ever stops
    # calling streamable_http_client directly (e.g. everything moves onto
    # ADK's own connection params), this should be revisited rather than
    # silently deleted.
    hits = sum(len(_CALL.findall(path.read_text())) for path in SRC_ROOT.rglob("*.py"))
    assert hits >= 2
