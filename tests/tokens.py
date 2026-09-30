"""A test token issuer: an RSA key, its key set, and tokens signed with it."""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from pulse.adapters.tokens import JwtVerifier
from pulse.entities.clock import Clock

ISSUER = "https://issuer.test"
AUDIENCE = "pulse-test"
KEY_ID = "test-key"
REVIEWER_ROLE = "agent.pulse"
REVIEWER_OID = "3f1c0a52-7d4e-4b8a-9c61-2e5f8d9a0b17"
NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def write_jwks(path: Path) -> Path:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    path.write_text(json.dumps({"keys": [jwk | {"kid": KEY_ID, "alg": "RS256", "use": "sig"}]}))
    return path


def make_token(
    *,
    signed_by_other_key: bool = False,
    kid: str = KEY_ID,
    algorithm: str = "RS256",
    drop: tuple[str, ...] = (),
    **claims: object,
) -> str:
    """A token for the reviewer, valid at NOW; keyword arguments override its claims."""
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "pairwise-subject",
        "oid": REVIEWER_OID,
        "roles": [REVIEWER_ROLE],
        "iat": NOW - timedelta(minutes=5),
        "nbf": NOW - timedelta(minutes=5),
        "exp": NOW + timedelta(hours=1),
    } | claims
    for claim in drop:
        del payload[claim]
    if algorithm == "HS256":
        return jwt.encode(payload, "shared-secret-" * 4, algorithm="HS256", headers={"kid": kid})
    key = _OTHER_KEY if signed_by_other_key else _KEY
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})


# One key set file for tests that only need a working verifier; removed when the run ends.
_KEY_SET_DIRECTORY = tempfile.TemporaryDirectory()
_JWKS_PATH = write_jwks(Path(_KEY_SET_DIRECTORY.name) / "jwks.json")


def make_verifier(clock: Clock) -> JwtVerifier:
    return JwtVerifier(ISSUER, AUDIENCE, str(_JWKS_PATH), clock)
