# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the CustomMessage trigger (no AWS calls).

Only the admin-create-user invitation changes; every other message keeps
Cognito's wording. An invitation for a meeting's participant names the meeting
and links to its consent page, with a link built from the web UI's address and
a checked query only, and the meeting's name escaped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
HERE = Path(__file__).resolve().parent


def _load_index():
    spec = importlib.util.spec_from_file_location("custom_message_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()
URL = "https://d111111abcdef8.cloudfront.net"


class FakeSsm:
    def __init__(self, value):
        self.value = value

    def get_parameter(self, Name):  # noqa: N803
        return {"Parameter": {"Value": json.dumps(self.value)}}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(index, "PARAM_NAME", "LMASettings")
    monkeypatch.setattr(index, "ssm", FakeSsm({"CloudFrontEndpoint": URL}))


def invitation(metadata=None, trigger="CustomMessage_AdminCreateUser"):
    request = {"codeParameter": "{####}", "usernameParameter": "ana@example.com"}
    if metadata is not None:
        request["clientMetadata"] = metadata
    return index.lambda_handler({"triggerSource": trigger, "request": request}, None)


def test_other_messages_keep_cognitos_wording():
    for trigger in (
        "CustomMessage_ForgotPassword",
        "CustomMessage_ResendCode",
        "CustomMessage_SignUp",
    ):
        event = invitation({"lmaInvitation": "meeting_participant"}, trigger=trigger)
        assert "response" not in event


def test_the_plain_invitation_has_no_meeting_paragraph():
    message = invitation()["response"]["emailMessage"]
    assert f'<a href="{URL}">{URL}</a>' in message
    assert "{####}" in message
    assert "consent" not in message


def test_a_participants_invitation_names_the_meeting_and_links_its_consent_page():
    message = invitation(
        {
            "lmaInvitation": "meeting_participant",
            "meetingName": "Weekly <sync> & review",
            "consentQuery": "meeting=Weekly%20sync%20-%202026-11-30",
        }
    )["response"]["emailMessage"]
    assert "the meeting <strong>Weekly &lt;sync&gt; &amp; review</strong>" in message
    link = f"{URL}/#/consents?meeting=Weekly%20sync%20-%202026-11-30"
    assert f'<a href="{link}">{link}</a>' in message
    assert "{####}" in message


def test_a_vp_query_is_linked_too():
    message = invitation(
        {"lmaInvitation": "meeting_participant", "meetingName": "", "consentQuery": "vp=abc-123"}
    )["response"]["emailMessage"]
    assert "a meeting" in message and f"{URL}/#/consents?vp=abc-123" in message


@pytest.mark.parametrize(
    "query",
    [
        "redirect=https://example.com",
        'meeting=x"><script>',
        "meeting=a&vp=b",
        "meeting=",
        None,
    ],
)
def test_a_query_of_any_other_shape_adds_no_link(query):
    message = invitation(
        {"lmaInvitation": "meeting_participant", "meetingName": "M", "consentQuery": query}
    )["response"]["emailMessage"]
    assert "/#/consents" not in message and "script" not in message


def test_no_link_before_the_web_ui_address_is_known(monkeypatch):
    monkeypatch.setattr(index, "ssm", FakeSsm({}))
    message = invitation(
        {"lmaInvitation": "meeting_participant", "meetingName": "M", "consentQuery": "vp=a"}
    )["response"]["emailMessage"]
    assert "Outputs tab" in message and "/#/consents" not in message


def test_a_long_meeting_name_is_cut():
    message = invitation(
        {"lmaInvitation": "meeting_participant", "meetingName": "x" * 500, "consentQuery": "vp=a"}
    )["response"]["emailMessage"]
    assert "x" * 200 in message and "x" * 201 not in message
