import io
import json
import time
from copy import deepcopy
from types import SimpleNamespace
from urllib.request import OpenerDirector

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient

from kb_assistant.adapters.oidc import OidcVerifier
from kb_assistant.errors import UnauthorizedError


@pytest.fixture
def auth_keys(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="one", use="sig")
    calls = []

    def fetch(*args, **kwargs):
        calls.append(True)
        data = {"keys": [deepcopy(jwk)]}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(OpenerDirector, "open", fetch)
    verifier = OidcVerifier("https://issuer", "https://issuer/jwks", "client", "kb-api/access")

    def token(changes=None, algorithm="RS256", kid="one"):
        claims = dict(
            sub="owner",
            iss="https://issuer",
            exp=int(time.time()) + 600,
            iat=int(time.time()),
            client_id="client",
            token_use="access",
            scope="openid kb-api/access",
        )
        claims.update(changes or {})
        claims = {k: v for k, v in claims.items() if v is not None}
        signing_key = (
            key if algorithm == "RS256" else ("secret" * 8 if algorithm == "HS256" else None)
        )
        return "Bearer " + jwt.encode(
            claims, signing_key, algorithm=algorithm, headers={"kid": kid}
        )

    return verifier, token, calls, jwk


@pytest.mark.parametrize("header", [None, "", "bearer x", "Bearer ", "Basic x"])
async def test_missing_malformed_bearer(auth_keys, header):
    with pytest.raises(UnauthorizedError):
        await auth_keys[0].verify(header)


@pytest.mark.parametrize(
    "changes,username",
    [
        ({"username": "alice"}, "alice"),
        (
            {
                "token_use": None,
                "typ": "Bearer",
                "client_id": None,
                "azp": "client",
                "preferred_username": "bob",
                "aud": "unrelated",
            },
            "bob",
        ),
        ({}, "owner"),
    ],
)
async def test_valid_access_token_styles(auth_keys, changes, username):
    verifier, token, _, _ = auth_keys
    result = await verifier.verify(token(changes))
    assert result.sub == "owner" and result.username == username


@pytest.mark.parametrize(
    "changes",
    [
        {"token_use": "id"},
        {"token_use": None, "typ": "ID"},
        {"token_use": None},
        {"exp": None},
        {"iat": None},
        {"sub": None},
        {"exp": 1},
        {"iss": "wrong"},
        {"client_id": "wrong"},
        {"client_id": "wrong", "azp": "client"},
        {"scope": "openid"},
        {"iat": int(time.time()) + 900},
    ],
)
async def test_invalid_claims_rejected(auth_keys, changes):
    verifier, token, _, _ = auth_keys
    with pytest.raises(UnauthorizedError):
        await verifier.verify(token(changes))


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
async def test_invalid_algorithms(auth_keys, algorithm):
    with pytest.raises(UnauthorizedError):
        await auth_keys[0].verify(auth_keys[1](algorithm=algorithm))


async def test_unknown_kid_refreshes_once_and_rotation(auth_keys, monkeypatch):
    verifier, token, calls, jwk = auth_keys
    clock = [time.monotonic()]
    monkeypatch.setattr("jwt.jwks_client.time", SimpleNamespace(monotonic=lambda: clock[0]))
    await verifier.verify(token())
    clock[0] += 10.1
    with pytest.raises(UnauthorizedError):
        await verifier.verify(token(kid="missing"))
    assert len(calls) == 2
    with pytest.raises(UnauthorizedError):
        await verifier.verify(token(kid="another-missing"))
    assert len(calls) == 2
    jwk["kid"] = "rotated"
    clock[0] += 10.1
    assert (await verifier.verify(token(kid="rotated"))).sub == "owner"
    assert len(calls) == 3


@pytest.mark.parametrize("field", ["iat", "nbf"])
async def test_small_clock_skew_without_expiry_leeway(auth_keys, field):
    verifier, token, _, _ = auth_keys
    assert (await verifier.verify(token({field: time.time() + 4}))).sub == "owner"
    with pytest.raises(UnauthorizedError):
        await verifier.verify(token({field: time.time() + 7}))
    with pytest.raises(UnauthorizedError):
        await verifier.verify(token({field: time.time() + 4, "exp": time.time() - 1}))


@pytest.mark.parametrize("iat", ["123", "bad", True, [], float("nan"), float("inf")])
async def test_iat_must_be_numeric(auth_keys, iat):
    with pytest.raises(UnauthorizedError):
        await auth_keys[0].verify(auth_keys[1]({"iat": iat}))


async def test_jwks_outage_fails_closed(auth_keys, monkeypatch, capsys):
    def outage(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(PyJWKClient, "fetch_data", outage)
    with pytest.raises(UnauthorizedError):
        await auth_keys[0].verify(auth_keys[1]())
    assert "token verification failed" in capsys.readouterr().out


async def test_hanging_jwks_endpoint_fails_closed_within_timeout(auth_keys, monkeypatch, capsys):
    import socket
    import threading

    monkeypatch.undo()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
    host, port = listener.getsockname()
    verifier = OidcVerifier("https://issuer", f"http://{host}:{port}/jwks", "client")
    started = time.monotonic()
    try:
        with pytest.raises(UnauthorizedError):
            await verifier.verify(auth_keys[1]())
    finally:
        listener.close()
    assert time.monotonic() - started < 5
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["level"] for r in records if r["msg"] == "token verification failed"] == ["error"]


async def test_rejected_tokens_are_not_error_level(auth_keys, capsys):
    verifier, token, _, _ = auth_keys
    for header in ["Bearer not-a-jwt", token({"exp": 1}), token({"client_id": "wrong"})]:
        with pytest.raises(UnauthorizedError):
            await verifier.verify(header)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records and {r["level"] for r in records} == {"info"}
    assert all(set(r) <= {"level", "msg", "errorType", "requestId", "userSub"} for r in records)
