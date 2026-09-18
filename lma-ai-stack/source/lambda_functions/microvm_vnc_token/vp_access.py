# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Ownership checks shared by the VNC token-minting resolvers.

Both resolvers that hand out VNC credentials must agree on exactly who may view
a Virtual Participant, so the check lives in one module. Lambda packages are
built per ``CodeUri`` and cannot import across directories, so this file is
copied verbatim into each function's source directory -- the same convention
``microvm_client.py`` already follows in this tree. The copies are asserted
byte-identical by ``test_vp_access_copies_are_identical`` in
``vnc_edge_token/test_index.py``, so a change in one is a test failure until
it is applied to the other.

The VP id on its own is not a credential: it appears in URLs and logs. Access is
decided from the caller identity AppSync supplies, checked against the VP
record's own ``Owner`` / ``SharedWith`` attributes.
"""

from __future__ import annotations


def caller_identity(identity: dict) -> str:
    """The caller's identity string, or "" when AppSync supplied none.

    The precedence matches virtual_participant_manager. ``identity.username``
    must stay in the chain: a live call failed because only the claims were
    consulted and that deployment populates ``identity.username`` instead.
    """
    claims = identity.get("claims", {}) or {}
    return claims.get("email") or claims.get("cognito:username") or identity.get("username") or ""


def caller_groups(identity: dict) -> list:
    """The caller's Cognito groups, normalised to a list."""
    claims = identity.get("claims", {}) or {}
    groups = identity.get("groups") or claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    return list(groups)


def authorize_vp_access(dynamodb, table_name: str, vp_id: str, identity: dict, logger) -> dict:
    """Return the VP's DynamoDB item once the caller is confirmed to have access.

    Raises when the caller has no identity, the VP does not exist, or the caller
    neither owns the VP, has had it shared with them, nor is an Admin.

    :param dynamodb: a boto3 ``dynamodb`` client (passed in so callers can stub it)
    """
    identity = identity or {}
    caller = caller_identity(identity)
    if not caller:
        logger.error(
            "No caller identity; claims=%s identity keys=%s",
            sorted(identity.get("claims", {}) or {}),
            sorted(identity),
        )
        raise Exception("Unauthenticated")

    vp = dynamodb.get_item(TableName=table_name, Key={"id": {"S": vp_id}}).get("Item")
    if not vp:
        raise Exception(f"Virtual Participant {vp_id} not found")

    # Field names and semantics match the canonical subscription filter in
    # source/appsync/subscription.js: Owner (capital O) equals identity.username,
    # and SharedWith CONTAINS it. SharedWith is a comma-ish String, not a List --
    # reading it as a List (and "owner" lowercase) made every request fail
    # "Not authorized", because both lookups silently returned empty.
    owner = vp.get("Owner", {}).get("S", "") or vp.get("owner", {}).get("S", "")
    shared_raw = vp.get("SharedWith", {}).get("S", "")
    shared = {v.strip() for v in shared_raw.split(",") if v.strip()}

    # Admins may view any VP, matching the subscription filter's group check.
    is_admin = "Admin" in caller_groups(identity)

    if not is_admin and caller != owner and caller not in shared:
        logger.warning(
            "Caller %s is not authorized for VP %s (owner=%s shared=%s)",
            caller,
            vp_id,
            owner,
            sorted(shared),
        )
        raise Exception("Not authorized for this Virtual Participant")

    return vp
