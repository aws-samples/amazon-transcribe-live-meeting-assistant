# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Cognito CustomMessage trigger for LMA: the invitation a new account is sent.

Only the admin-create-user invitation (``CustomMessage_AdminCreateUser``) is
changed; every other message (forgot password, verify email, resend code and
so on) is returned untouched, so Cognito's own wording applies.

The invitation gives the clickable LMA web UI (CloudFront) address, read from
the LMA Settings SSM parameter, which the AI stack fills in once the
distribution exists. Until then (the first Admin is created with the stack,
before it), the message points to the stack's Outputs tab instead.

An account created for a person on a meeting's participant list
(``createParticipantAccounts``) is sent with client metadata naming the
meeting: ``lmaInvitation`` ``meeting_participant``, ``meetingName`` and
``consentQuery`` (``meeting=<CallId>`` or ``vp=<Virtual Participant id>``,
URL-encoded). Its invitation then also names the meeting and links to the
meeting's consent page, where the person can give consent in advance once
signed in. The link is built here from the web UI's address and the checked
query, never taken whole from the metadata.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

PARAM_NAME = os.environ.get("LMA_SETTINGS_PARAMETER", "")
STACK_NAME = os.environ.get("LMA_STACK_NAME", "")
VERSION = os.environ.get("LMA_VERSION", "")
BUILD_DT = os.environ.get("LMA_BUILD_DATETIME", "")

#: The web UI route of the consent page, as lma-ai-stack/source/ui/src/routes/constants.js names it.
CONSENT_ROUTE = "/#/consents"
#: The query naming the meeting: one parameter, URL-encoded, as the backend sends it.
_CONSENT_QUERY = re.compile(r"^(meeting|vp)=[A-Za-z0-9%._~+\-]{1,1500}$")
MAX_MEETING_NAME = 200


def _get_cloudfront_url():
    if not PARAM_NAME:
        return ""
    try:
        resp = ssm.get_parameter(Name=PARAM_NAME)
        raw = resp.get("Parameter", {}).get("Value", "")
        if not raw:
            return ""
        settings = json.loads(raw)
        return (settings.get("CloudFrontEndpoint") or "").strip()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not read LMA settings parameter: %s", exc)
        return ""


def _meeting_paragraph(metadata, url):
    """The invitation's paragraph about the meeting, or "" when it names none."""
    if not isinstance(metadata, dict) or metadata.get("lmaInvitation") != "meeting_participant":
        return ""
    query = metadata.get("consentQuery")
    if not url or not isinstance(query, str) or not _CONSENT_QUERY.match(query):
        return ""
    name = metadata.get("meetingName")
    name = name.strip()[:MAX_MEETING_NAME] if isinstance(name, str) else ""
    link = html.escape(f"{url.rstrip('/')}{CONSENT_ROUTE}?{query}", quote=True)
    meeting = f"the meeting <strong>{html.escape(name)}</strong>" if name else "a meeting"
    return (
        f"<p>You have been listed as a participant in {meeting}. Once you have signed in, "
        "you can say on its consent page whether you agree to its being recorded and "
        "transcribed with Live Meeting Assistant, before the meeting starts: "
        f'<a href="{link}">{link}</a></p>'
    )


def lambda_handler(event, _context):
    trigger = event.get("triggerSource", "")
    logger.info("CustomMessage trigger %s", trigger)
    # Only customize the admin-create-user invitation email.
    if trigger != "CustomMessage_AdminCreateUser":
        return event

    req = event.get("request", {}) or {}
    code = req.get("codeParameter", "{####}")
    username = html.escape(req.get("usernameParameter", "") or "")
    url = _get_cloudfront_url()
    meeting = _meeting_paragraph(req.get("clientMetadata"), url)

    event.setdefault("response", {})
    event["response"]["emailSubject"] = "Welcome to Live Meeting Assistant!"

    footer = (
        "<p>Thanks,</p>"
        "<p>Live Meeting Assistant (amazon.com/live-meeting-assistant)</p>"
        f"<p><em>Stack: {html.escape(STACK_NAME)}, v{html.escape(VERSION)}, "
        f"{html.escape(BUILD_DT)}</em></p>"
    )
    if url:
        safe_url = html.escape(url, quote=True)
        event["response"]["emailMessage"] = (
            f"<p>Hello {username},</p>"
            "<p>Welcome to Live Meeting Assistant (LMA)! Your temporary password is: "
            f"<strong>{code}</strong></p>"
            f'<p>Sign in to LMA at <a href="{safe_url}">{safe_url}</a>. '
            "You'll be prompted to set a permanent password on first sign-in.</p>"
            f"{meeting}{footer}"
        )
    else:
        # AISTACK hasn't populated CloudFrontEndpoint yet (e.g. initial stack creation).
        event["response"]["emailMessage"] = (
            f"<p>Hello {username},</p>"
            "<p>Welcome to Live Meeting Assistant (LMA)! Your temporary password is: "
            f"<strong>{code}</strong></p>"
            "<p>When the CloudFormation stack is COMPLETE, navigate to the Outputs tab of your "
            "CloudFormation stack and get the URL associated with ApplicationCloudfrontEndpoint. "
            "This is your LMA web interface. When you first login, it will prompt you to set a "
            "permanent password.</p>"
            "<p>Don't attempt to download the Chrome browser extension from LMA until the "
            "CloudFormation stack status is CREATE_COMPLETE or UPDATE_COMPLETE, as the "
            "extension will not be available until then.</p>"
            f"{footer}"
        )
    return event
