import asyncio
import math
import time

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from kb_assistant.core.ports import AuthContext
from kb_assistant.errors import UnauthorizedError
from kb_assistant.logging import log


class OidcVerifier:
    def __init__(
        self, issuer: str, jwks_url: str, client_id: str, required_scope: str = ""
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.required_scope = required_scope
        # A short fetch timeout keeps a hung JWKS endpoint from outlasting 15 s functions.
        self.jwks = PyJWKClient(
            jwks_url, cache_jwk_set=True, lifespan=300, timeout=3, cooldown_duration=10
        )

    async def verify(self, authorization_header: str | None) -> AuthContext:
        if authorization_header is None or not authorization_header.startswith("Bearer "):
            raise UnauthorizedError()
        token = authorization_header[7:].strip()
        if not token:
            raise UnauthorizedError()
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    "verify_aud": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
                leeway=0,
            )
            # Only issuance/not-before tolerate skew; expiration remains strict.
            for field in ("iat", "nbf"):
                if field not in claims:
                    continue
                value = claims[field]
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise UnauthorizedError()
                if value > time.time() + 5:
                    raise UnauthorizedError()
            if "token_use" in claims:
                if claims["token_use"] != "access":
                    raise UnauthorizedError()
            elif claims.get("typ") != "Bearer":
                raise UnauthorizedError()
            if claims.get("client_id", claims.get("azp")) != self.client_id:
                raise UnauthorizedError()
            if self.required_scope and self.required_scope not in claims.get("scope", "").split():
                raise UnauthorizedError()
            username = claims.get("username", claims.get("preferred_username", claims["sub"]))
            return AuthContext(
                claims["sub"], username if isinstance(username, str) else claims["sub"]
            )
        except Exception as exc:
            # Rejected tokens are routine on a public URL; only key-fetch failures are errors.
            key_fetch = isinstance(exc, (PyJWKClientConnectionError, OSError))
            log(
                "error" if key_fetch else "info",
                "token verification failed",
                errorType=type(exc).__name__,
            )
            raise UnauthorizedError() from exc
