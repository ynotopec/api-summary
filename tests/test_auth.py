import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api_summary.auth import Authenticator
from api_summary.config import Settings


def test_production_cannot_disable_auth():
    with pytest.raises(ValueError):
        Settings(auth_enabled=False)
    with pytest.raises(ValueError):
        Settings(
            oidc_issuer="https://sso.example.org",
            public_base_url="https://summary.example.org",
            oidc_algorithms=["HS256"],
        )


async def test_signed_tokens_audience_issuer_expiry_scope(settings):
    settings.auth_enabled = True
    settings.oidc_issuer = "https://sso.example.org"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authenticator(settings)
    auth.keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    claims = {
        "iss": settings.oidc_issuer,
        "sub": "alice",
        "aud": settings.oidc_audience,
        "scope": settings.oidc_scopes,
        "exp": time.time() + 60,
    }

    def encode(payload):
        return jwt.encode(payload, key, algorithm="RS256")

    alice = await auth.authenticate(encode(claims))
    bob = await auth.authenticate(encode({**claims, "sub": "bob"}))
    assert alice != bob
    for mutation in ({"aud": "other"}, {"iss": "other"}, {"exp": 1}):
        with pytest.raises(jwt.PyJWTError):
            await auth.authenticate(encode({**claims, **mutation}))
    with pytest.raises(PermissionError):
        await auth.authenticate(encode({**claims, "scope": "summary:read"}))
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.PyJWTError):
        await auth.authenticate(jwt.encode(claims, wrong, algorithm="RS256"))
