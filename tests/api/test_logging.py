import json
import logging

from src.api.logging import JsonFormatter, new_request_id, request_id_var


def test_new_request_id_is_unique_and_short():
    a, b = new_request_id(), new_request_id()
    assert a != b
    assert len(a) == 32


def test_formatter_emits_json_with_request_id():
    request_id_var.set("abc123")
    record = logging.LogRecord(
        name="cygnus.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cygnus.test"
    assert payload["request_id"] == "abc123"


def test_formatter_tolerates_missing_request_id():
    request_id_var.set("")
    record = logging.LogRecord(
        name="cygnus.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="no id",
        args=(),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] is None
