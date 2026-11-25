# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Altered and self-made JSON Web Tokens, for test_security_access.py.

Every entry point of a deployed stack that accepts a user pool token must accept
only a token the user pool signed, for this pool, that has not expired. These
helpers build the tokens it must refuse, from a genuine one:

* ``with_claims``: the payload changed (a role group added), the signature kept;
* ``with_altered_signature``: the payload kept, the signature changed;
* ``unsigned``: the ``alg`` header set to ``none`` and the signature dropped;
* ``self_signed``: the same claims signed with a key the test made, under a
  ``kid`` the pool does not publish, optionally naming another issuer.

They are pure (no AWS, no network) and are checked offline by
test_security_helpers.py.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def split(token: str) -> tuple[dict, dict, str]:
    """The header, the claims and the signature segment of a compact JWS."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a compact JWS: expected three dot-separated segments")
    header = json.loads(b64url_decode(parts[0]))
    claims = json.loads(b64url_decode(parts[1]))
    return header, claims, parts[2]


def _segment(value: dict) -> str:
    return b64url_encode(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def with_claims(token: str, **changes: Any) -> str:
    """The token with its claims changed and its original signature kept."""
    header, claims, signature = split(token)
    claims.update(changes)
    return f"{_segment(header)}.{_segment(claims)}.{signature}"


def with_altered_signature(token: str) -> str:
    """The token with the same header and claims and a signature that does not match them."""
    header_segment, claims_segment, signature = token.split(".")
    raw = bytearray(b64url_decode(signature))
    if not raw:
        raw = bytearray(b"\x00")
    raw[len(raw) // 2] ^= 0xFF
    return f"{header_segment}.{claims_segment}.{b64url_encode(bytes(raw))}"


def unsigned(token: str) -> str:
    """The token's claims under an ``alg: none`` header, with no signature."""
    header, claims, _ = split(token)
    header = {**header, "alg": "none"}
    return f"{_segment(header)}.{_segment(claims)}."


def self_signed(token: str, issuer: str | None = None, **changes: Any) -> str:
    """The token's claims signed RS256 with a new key, under a key id the pool does not publish.

    ``issuer`` replaces the ``iss`` claim when given. Needs the ``cryptography``
    package, which pycognito's JWT dependency installs.
    """
    from cryptography.hazmat.primitives import hashes  # pylint: disable=import-outside-toplevel
    from cryptography.hazmat.primitives.asymmetric import padding, rsa  # pylint: disable=import-outside-toplevel

    header, claims, _ = split(token)
    claims.update(changes)
    if issuer is not None:
        claims["iss"] = issuer
    header = {"alg": "RS256", "kid": f"integ-{secrets.token_hex(8)}", "typ": "JWT"}
    signing_input = f"{_segment(header)}.{_segment(claims)}"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url_encode(signature)}"


def other_issuer(issuer: str) -> str:
    """An issuer URL in the same Region naming a user pool id that does not exist."""
    base, _, pool_id = issuer.rstrip("/").rpartition("/")
    region = pool_id.split("_", 1)[0] if "_" in pool_id else "us-east-1"
    return f"{base}/{region}_IntegNoSuchPool"
