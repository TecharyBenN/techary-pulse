import threading
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pulse.adapters.tokens import JwtVerifier
from pulse.entities.auth import Caller
from pulse.entities.errors import InvalidToken
from tests.fakes.clock import ControlledClock
from tests.tokens import (
    AUDIENCE,
    ISSUER,
    NOW,
    REVIEWER_OID,
    REVIEWER_ROLE,
    make_token,
    write_jwks,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(params=["path", "url"])
def jwks(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """The key set as a file path, and as a URL served from this machine, as an issuer does."""
    path = write_jwks(tmp_path / "jwks.json")
    if request.param == "path":
        yield str(path)
        return
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/jwks.json"
    finally:
        server.shutdown()
        server.server_close()


def _verifier(jwks: str, clock: ControlledClock | None = None) -> JwtVerifier:
    return JwtVerifier(ISSUER, AUDIENCE, jwks, clock or ControlledClock(NOW))


async def test_valid_token_gives_its_caller(jwks: str) -> None:
    caller = await _verifier(jwks).verify(make_token())

    assert caller == Caller(oid=REVIEWER_OID, roles=[REVIEWER_ROLE])


async def test_token_without_roles_has_none(jwks: str) -> None:
    caller = await _verifier(jwks).verify(make_token(drop=("roles",)))

    assert caller.roles == []


@pytest.mark.parametrize(
    "token",
    [
        make_token(iss="https://other-issuer.test"),
        make_token(aud="another-api"),
        make_token(aud=["another-api", "ai-platform"]),
        make_token(signed_by_other_key=True),
        make_token(kid="unknown-key"),
        make_token(algorithm="HS256"),
        make_token(drop=("oid",)),
        make_token(drop=("exp",)),
        make_token(drop=("iss",)),
        make_token(drop=("aud",)),
        make_token(roles="agent.pulse"),
        "not-a-token",
        "",
    ],
    ids=[
        "wrong issuer",
        "wrong audience",
        "wrong audiences",
        "wrong key",
        "unknown key ID",
        "shared-secret algorithm",
        "no oid",
        "no expiry",
        "no issuer",
        "no audience",
        "roles not a list",
        "malformed",
        "empty",
    ],
)
async def test_invalid_token_is_refused(jwks: str, token: str) -> None:
    with pytest.raises(InvalidToken):
        await _verifier(jwks).verify(token)


async def test_token_with_one_matching_audience_is_accepted(jwks: str) -> None:
    caller = await _verifier(jwks).verify(make_token(aud=["ai-platform", AUDIENCE]))

    assert caller.oid == REVIEWER_OID


@pytest.mark.parametrize(
    ("offset", "valid"),
    [
        (timedelta(minutes=59), True),
        (timedelta(hours=1), False),
        (timedelta(hours=2), False),
        (-timedelta(minutes=5), True),
        (-timedelta(minutes=6), False),
    ],
)
async def test_expiry_and_start_are_checked_against_the_clock(
    jwks: str, offset: timedelta, valid: bool
) -> None:
    verifier = _verifier(jwks, ControlledClock(NOW + offset))

    if valid:
        assert (await verifier.verify(make_token())).oid == REVIEWER_OID
    else:
        with pytest.raises(InvalidToken):
            await verifier.verify(make_token())


async def test_missing_key_set_refuses_every_token(tmp_path: Path) -> None:
    with pytest.raises(InvalidToken):
        await _verifier(str(tmp_path / "missing.json")).verify(make_token())


async def test_key_set_file_is_read_again_after_rotation(tmp_path: Path) -> None:
    path = tmp_path / "jwks.json"
    path.write_text('{"keys": []}')
    verifier = _verifier(str(path))
    with pytest.raises(InvalidToken):
        await verifier.verify(make_token())

    write_jwks(path)

    assert (await verifier.verify(make_token())).oid == REVIEWER_OID
