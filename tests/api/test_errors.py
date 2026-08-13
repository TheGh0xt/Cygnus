import json

from src.api.errors import ErrorType, PmieError, problem_response


def test_problem_response_shape_and_media_type():
    exc = PmieError(ErrorType.EVENT_NOT_FOUND, "no event with slug 'nope'", status=404)
    response = problem_response(exc)
    assert response.status_code == 404
    assert response.media_type == "application/problem+json"
    body = json.loads(response.body)
    assert body["type"] == "https://pmie.dev/problems/event-not-found"
    assert body["status"] == 404
    assert body["detail"] == "no event with slug 'nope'"
    assert body["title"]


def test_every_error_type_has_a_stable_slug():
    # The slug is the client's stable switch key — it must never be the
    # human-readable title, which is free to be reworded.
    for member in ErrorType:
        assert member.value.replace("-", "").isalpha()


def test_pmie_error_carries_its_status():
    exc = PmieError(ErrorType.RATE_LIMITED, "slow down", status=429)
    assert exc.status == 429
    assert exc.error_type is ErrorType.RATE_LIMITED
