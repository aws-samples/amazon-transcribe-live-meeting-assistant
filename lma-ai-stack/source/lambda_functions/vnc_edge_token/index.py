# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Mint a short-lived, VP-scoped access token for the /vnc/* CloudFront path.

The viewer reaches an ECS-hosted Virtual Participant through
``wss://<cloudfront-domain>/vnc/<vpId>``, where a Lambda@Edge viewer-request
function decides whether to forward the request to the VNC ALB. This resolver
mints the token that function checks.

Two invariants hold by construction:

* the token is issued only after the caller's access to this VP has been
  confirmed against the VP record (see ``vp_access``), so possession of a VP id
  is not enough to obtain one;
* the signed payload names the single VP and path prefix the token is good for,
  and carries a few-minute expiry, so it is usable only for the request the
  viewer is about to make.

The signature is HMAC-SHA256 over the encoded payload, keyed with a secret
generated once per deployment and held in Secrets Manager. The edge function
cannot use layers, so both sides deliberately stay on ``hmac``/``hashlib``/
``json``/``base64``/``time`` for the token format itself.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from vp_access import authorize_vp_access

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb")
secretsmanager = boto3.client("secretsmanager")

# The viewer connects immediately after minting and re-mints on every reconnect
# (see vncConnection.js), so the token only has to outlive one WebSocket
# handshake. Keeping it to minutes bounds how long a copied URL stays usable.
TOKEN_TTL_SECONDS = 300

# websockify serves noVNC here inside the VP container, and the ALB target group
# points at it. Naming the port in the payload keeps the token tied to the one
# service the viewer needs.
NOVNC_PORT = 5901

# One cached copy per warm container. The secret is generated once per
# deployment, so there is nothing to refresh.
_signing_secret = []


def get_signing_secret():
    """The deployment's token signing secret, read once per container."""
    if not _signing_secret:
        response = secretsmanager.get_secret_value(SecretId=os.environ["SIGNING_SECRET_ARN"])
        _signing_secret.append(response["SecretString"])
    return _signing_secret[0]


def b64url(raw: bytes) -> str:
    """base64url without padding, matching the edge function's decoder."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint_token(secret: str, vp_id: str, expires_at_epoch: int) -> str:
    """Build ``base64url(payload).base64url(HMAC-SHA256(secret, payload))``.

    The MAC covers the *encoded* payload rather than the decoded JSON, so the
    edge function never has to reproduce this serialization byte-for-byte in
    order to check the signature.
    """
    payload = json.dumps(
        {
            "vpId": vp_id,
            "exp": expires_at_epoch,
            "prefix": f"/vnc/{vp_id}",
            "port": NOVNC_PORT,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_b64 = b64url(payload)
    signature = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
    ).digest()
    return f"{payload_b64}.{b64url(signature)}"


def lambda_handler(event, context):
    args = event.get("arguments", {}) or {}
    vp_id = args.get("vpId")
    if not vp_id:
        raise Exception("vpId is required")

    vp = authorize_vp_access(
        dynamodb,
        os.environ["VP_TABLE_NAME"],
        vp_id,
        event.get("identity", {}) or {},
        logger,
    )

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)
    token = mint_token(get_signing_secret(), vp_id, int(expires_at.timestamp()))

    # The VNC server itself also asks for a credential, which the VP task
    # generates per boot and records on its own VP item (see the VP stack's
    # entrypoint.sh). It travels back with the token so the viewer needs exactly
    # one authenticated round trip, and only to callers who just passed the
    # ownership check above. Absent for tasks that predate this field.
    vnc_password = vp.get("vncPassword", {}).get("S") or None

    # Neither the token nor the credential is logged.
    logger.info("Minted a VNC access token for VP %s", vp_id)
    return {
        "token": token,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        "vncPassword": vnc_password,
    }
