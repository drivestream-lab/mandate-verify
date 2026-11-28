"""GitHubApp recovers from a refused token or App JWT once, and does not loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from email.utils import formatdate

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mandate_runner.github import GitHubApp
from mandate_runner.secrets import AppCredentials

TOKEN_PATH = "/app/installations/7/access_tokens"
STATUS_PATH = "/repos/o/r/statuses/" + "a" * 40


@pytest.fixture(scope="module")
def keys() -> tuple[bytes, object]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem, key.public_key()


def make_app(pem: bytes, handler: Callable[[httpx.Request], httpx.Response]) -> GitHubApp:
    creds = AppCredentials(app_id=5860839, installation_id=7, private_key=pem)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GitHubApp(creds, "mandate/build-verified", api="https://gh.test", client=client)


def token_body(n: int) -> dict[str, str]:
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    return {"token": f"ghs_test{n}", "expires_at": expires}


def test_refused_installation_token_is_replaced_once(keys):
    pem, _ = keys
    minted: list[int] = []
    used: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == TOKEN_PATH:
            minted.append(1)
            return httpx.Response(201, json=token_body(len(minted)))
        used.append(req.headers["Authorization"])
        if req.headers["Authorization"] == "token ghs_test1":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(201, json={})

    app = make_app(pem, handler)
    app.post_status("o/r", "a" * 40, "success", "ok", None)

    assert len(minted) == 2
    assert used == ["token ghs_test1", "token ghs_test2"]


def test_a_second_401_is_raised_not_retried(keys):
    pem, _ = keys
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == TOKEN_PATH:
            return httpx.Response(201, json=token_body(len(calls)))
        return httpx.Response(401, json={"message": "Bad credentials"})

    app = make_app(pem, handler)
    with pytest.raises(httpx.HTTPStatusError):
        app.post_status("o/r", "a" * 40, "success", "ok", None)

    assert calls == [TOKEN_PATH, STATUS_PATH, TOKEN_PATH, STATUS_PATH]


def test_app_jwt_is_retimed_to_githubs_clock(keys):
    pem, public = keys
    github_now = time.time() + 600  # our clock lags GitHub's by 10 minutes
    iats: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        claims = jwt.decode(
            req.headers["Authorization"].removeprefix("Bearer "),
            public,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        iats.append(claims["iat"])
        if claims["iat"] + 60 < github_now - 30:
            return httpx.Response(
                401,
                headers={"Date": formatdate(github_now, usegmt=True)},
                json={"message": "'Expiration time' claim ('exp') is too far in the past"},
            )
        return httpx.Response(201, json=token_body(1))

    app = make_app(pem, handler)
    assert app.installation_token() == "ghs_test1"
    assert len(iats) == 2
    assert abs((iats[1] + 60) - github_now) < 5


def test_jwt_401_with_matching_clock_is_not_retried(keys):
    pem, _ = keys
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(
            401,
            headers={"Date": formatdate(time.time(), usegmt=True)},
            json={"message": "A JSON web token could not be decoded"},
        )

    app = make_app(pem, handler)
    with pytest.raises(httpx.HTTPStatusError):
        app.installation_token()

    assert calls == [TOKEN_PATH]
