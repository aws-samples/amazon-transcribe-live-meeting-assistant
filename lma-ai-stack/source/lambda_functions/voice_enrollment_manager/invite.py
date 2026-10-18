# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Invite somebody to enrol their voice.

The person being enrolled is frequently not an LMA user -- somebody from another
company, a contractor in a meeting room, a candidate in an interview -- so they
need an identity of their own before they can consent for themselves. An admin
invites an email address, which creates a Cognito user in the `VoiceEnrollee`
group: that group can reach the enrollment portal and nothing else, no meetings
and no configuration.

What an invitation is NOT is a consent. It creates the ability for somebody to
decide, and records who asked them to. Declining is a normal outcome and leaves
no voiceprint. Only the invitee can grant, which `pure.may_grant` enforces.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import boto3
import pure
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

USER_POOL_ID = os.environ["USER_POOL_ID"]
CONSENT_LOG_TABLE = os.environ["VOICE_CONSENT_LOG_TABLE"]
TERMS_VERSION = os.environ.get("VOICE_ENROLLMENT_TERMS_VERSION", "1")
ALLOWED_SIGNUP_EMAIL_DOMAIN = os.environ.get("ALLOWED_SIGNUP_EMAIL_DOMAIN", "")

# The group an invitee lands in. It is granted nothing in the UI beyond the
# enrollment portal, so an invitation cannot be used to hand somebody access to
# meetings by the back door.
ENROLLEE_GROUP = "VoiceEnrollee"
ADMIN_GROUP = "Admin"

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

cognito = boto3.client("cognito-idp")
dynamodb = boto3.client("dynamodb")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def caller(event):
    """Who is asking, from the AppSync identity."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return {
        "sub": claims.get("sub") or identity.get("username") or "",
        "email": claims.get("email") or identity.get("username") or "",
        "groups": list(groups),
    }


def allowed_domains():
    """The deployment's permitted sign-up domains, as a list.

    `AllowedSignUpEmailDomain` is documented as "a domain or a comma separated
    list of domains", and the Cognito pre-sign-up trigger splits it. Comparing
    against the whole parameter as one string instead would refuse every
    address on a multi-domain deployment, including ones plainly on the list.
    """
    return [
        domain.strip().lower()
        for domain in ALLOWED_SIGNUP_EMAIL_DOMAIN.split(",")
        if domain.strip()
    ]


def validate_email(email):
    """A plausible address, within the deployment's allowed domains if it has any."""
    # fullmatch rather than match: Python's `$` also matches before a trailing
    # newline, so "someone@example.com\n" would otherwise pass here and then
    # fail the domain test for a reason that makes no sense to the admin.
    if not email or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("that does not look like an email address")
    allowed = allowed_domains()
    if allowed and email.split("@", 1)[1].lower() not in allowed:
        # Same rule the rest of the deployment applies to sign-ups. An
        # invitation must not be a way around it.
        raise ValueError(f"only {', '.join(allowed)} addresses can be invited")
    return email.strip()


def invite(event, who):
    """Create the invitee's account and record who invited them."""
    args = (event.get("arguments") or {}).get("input") or {}
    if not pure.may_grant(pure.ACTION_INVITED, args.get("Email", ""), who["sub"], who["groups"]):
        return {"Success": False, "Message": "only an administrator can invite"}

    try:
        email = validate_email((args.get("Email") or "").strip())
    except ValueError as error:
        return {"Success": False, "Message": str(error)}

    created = False
    try:
        response = cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        created = True
        subject_id = next(
            (a["Value"] for a in response["User"]["Attributes"] if a["Name"] == "sub"),
            email,
        )
    except cognito.exceptions.UsernameExistsException:
        # Already has an account -- which is fine, and common: an LMA user may
        # well want to enrol their own voice. Re-inviting just records the ask.
        LOGGER.info("Invitee already has an account; recording the invitation")
        existing = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
        subject_id = next(
            (a["Value"] for a in existing["UserAttributes"] if a["Name"] == "sub"), email
        )
    except ClientError as error:
        LOGGER.exception("Could not create the invitee's account")
        raise RuntimeError("could not send that invitation") from error

    try:
        cognito.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID, Username=email, GroupName=ENROLLEE_GROUP
        )
    except ClientError:
        # Roll back a user this invitation created, the way user_management does:
        # an account in no group can reach nothing and is just litter. An
        # account that already existed is left alone -- it is not ours to delete.
        # The address is not logged: an invitee's email identifies a person, and
        # the group name plus the stack trace is what diagnosing this needs.
        LOGGER.exception("Could not add an invitee to %s", ENROLLEE_GROUP)
        if created:
            try:
                cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
            except ClientError:
                LOGGER.exception("Could not roll back the invitee's account")
        raise RuntimeError("could not send that invitation")

    # Recorded in the same append-only log as the consent itself, so who asked
    # whom is as auditable as what they agreed to. An invitation carries no
    # biometric data and is not a consent.
    dynamodb.put_item(
        TableName=CONSENT_LOG_TABLE,
        Item={
            key: {"S": str(value)} if isinstance(value, str) else {"N": str(value)}
            for key, value in pure.consent_record(
                subject_id=subject_id,
                action=pure.ACTION_INVITED,
                terms_version=TERMS_VERSION,
                terms_text="",
                actor_sub=who["sub"],
                actor_email=who["email"],
                recorded_at=_now(),
            ).items()
        },
        ConditionExpression="attribute_not_exists(SubjectId) AND attribute_not_exists(RecordedAt)",
    )
    LOGGER.info("Invited an address to enrol, by %s", who["sub"])
    return {
        "Success": True,
        "Message": "invitation sent",
        "Status": pure.STATUS_PENDING,
    }


def handler(event, context):  # pylint: disable=unused-argument
    """AppSync resolver for inviteVoiceEnrollee."""
    who = caller(event)
    if not who["sub"]:
        return {"Success": False, "Message": "not permitted"}
    try:
        return invite(event, who)
    except Exception:  # noqa: BLE001 - reported, never leaked
        LOGGER.exception("Invitation failed")
        return {"Success": False, "Message": "could not send that invitation"}
