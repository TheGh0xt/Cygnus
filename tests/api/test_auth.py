import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from src.api.auth import (
    AuthError,
    CurrentUser,
    decode_token,
    extract_bearer_token,
)

# A throwaway EC keypair standing in for Supabase's signing key. Verification
# is asymmetric, so tests can mint tokens without any shared secret.
_KEY = ec.generate_private_key(ec.SECP256R1())
_KID = "test-key-1"


def _jwks():
    public_numbers = _KEY.public_key().public_numbers()

    def b64(value: int) -> str:
        import base64

        raw = value.to_bytes(32, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "alg": "ES256",
                "crv": "P-256",
                "kid": _KID,
                "kty": "EC",
                "use": "sig",
                "x": b64(public_numbers.x),
                "y": b64(public_numbers.y),
            }
        ]
    }


def _token(**overrides):
    claims = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "email": "tester@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, _KEY, algorithm="ES256", headers={"kid": _KID})


class TestExtractBearerToken:
    def test_extracts_token_from_header(self):
        assert extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_is_case_insensitive_on_the_scheme(self):
        assert extract_bearer_token("bearer abc") == "abc"

    @pytest.mark.parametrize(
        "header", [None, "", "abc.def", "Basic abc", "Bearer", "Bearer  "]
    )
    def test_rejects_anything_that_is_not_a_bearer_token(self, header):
        with pytest.raises(AuthError):
            extract_bearer_token(header)


class TestDecodeToken:
    def test_valid_token_yields_the_user(self):
        user = decode_token(_token(), _jwks())
        assert isinstance(user, CurrentUser)
        assert user.id == "11111111-2222-3333-4444-555555555555"
        assert user.email == "tester@example.com"

    def test_expired_token_is_rejected(self):
        with pytest.raises(AuthError):
            decode_token(_token(exp=int(time.time()) - 60), _jwks())

    def test_token_signed_by_another_key_is_rejected(self):
        other = ec.generate_private_key(ec.SECP256R1())
        forged = jwt.encode(
            {
                "sub": "attacker",
                "aud": "authenticated",
                "exp": int(time.time()) + 3600,
            },
            other,
            algorithm="ES256",
            headers={"kid": _KID},
        )
        with pytest.raises(AuthError):
            decode_token(forged, _jwks())

    def test_token_with_unknown_kid_is_rejected(self):
        token = jwt.encode(
            {"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
            _KEY,
            algorithm="ES256",
            headers={"kid": "not-a-known-key"},
        )
        with pytest.raises(AuthError):
            decode_token(token, _jwks())

    def test_wrong_audience_is_rejected(self):
        with pytest.raises(AuthError):
            decode_token(_token(aud="some-other-service"), _jwks())

    def test_token_without_a_subject_is_rejected(self):
        # No subject means no user to attribute usage or data to.
        with pytest.raises(AuthError):
            decode_token(_token(sub=None), _jwks())

    def test_unsigned_token_is_rejected(self):
        # The alg=none attack: a token asserting it needs no signature.
        forged = jwt.encode(
            {"sub": "attacker", "aud": "authenticated"}, key="", algorithm="none"
        )
        with pytest.raises(AuthError):
            decode_token(forged, _jwks())
