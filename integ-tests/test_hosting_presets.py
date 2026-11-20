# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The hosting presets, and deleting meetings by label, against a live stack.

See docs/hosting-models.md for the behaviour asserted.

Default run (no meeting needed, about two minutes, self-cleaning):

``test_each_hosting_preset_is_a_managed_profile_with_its_values``
    The seeder wrote ``team-hosted-external`` and ``customer-account`` as Managed
    profiles, each holding every value its preset sets. Read-only.
``test_a_dry_run_lists_a_users_own_labelled_meetings_and_deletes_nothing`` and the
tests after it
    Synthetic meetings are created through the call data stream, as a client's
    START creates one, carrying a unique label: two owned by a temporary user,
    one by another owner, one with another label value, and one still in
    progress. ``lma meetings delete`` is then run, through the CLI itself, as the
    temporary user (their own meetings) and as a temporary Admin (every user's):
    a dry run deletes nothing, a meeting in progress is skipped, and a confirmed
    run removes each matching meeting whole -- its record, its list entry, its
    transcript items and its files in the recordings bucket -- and nothing else.
    The two temporary Cognito users, created with no password and no invitation message, and
    every synthetic meeting are removed in teardown.

Opt-in, with ``LMA_TEST_HOSTING_ASSISTANT=1`` (changes a deployment-wide setting
while it runs, put back in teardown, waits up to seven minutes for it to apply,
and calls Amazon Bedrock for the control request):

``test_requests_made_in_the_meeting_are_refused_while_who_may_ask_is_off``
    With Meeting Assistant › In-meeting requests: who may ask set to Off in the
    active profile, a question sent as the meeting chat sends it is answered with
    the fixed refusal, and a question from the meeting page's chat is answered by
    the model.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from click.testing import CliRunner
from kds_pipeline_probe import cleanup
from live_meeting import read_meeting, wait_for
from lma_cli.cli import main
from meeting_consent_probe import Stack, _lma_config

MEETING_RECORD_TIMEOUT_S = 90.0
#: The owner of the meeting that is not the temporary user's.
OTHER_OWNER = "integ-test@lma.aws"
TRANSCRIPTS_PREFIX = "lma-transcripts/"
ARCHIVES_PREFIX = "lma-meeting-archives/"


@pytest.fixture(scope="module")
def stack(client) -> Stack:
    return Stack.find(client.stack_name, client.region)


# ── the Managed profiles ───────────────────────────────────────────────────


@pytest.mark.parametrize("preset_id", ["team-hosted-external", "customer-account"])
def test_each_hosting_preset_is_a_managed_profile_with_its_values(stack: Stack, preset_id: str) -> None:
    cfg = _lma_config()
    from lma_config.presets import preset_by_id  # pylint: disable=import-outside-toplevel

    store = cfg.ConfigurationStore(stack.configuration_table, client=stack.session.client("dynamodb"))
    record = store.read_profile(preset_id, consistent_read=True)
    assert record.exists, f"the deployment has no Managed profile {preset_id}; was it deployed from this branch?"
    assert record.metadata.get("Managed"), f"{preset_id} is not a Managed profile"
    for section, change in preset_by_id(preset_id)["Sections"].items():
        stored = record.body.get(section) or {}
        for key, value in change["values"].items():
            assert stored.get(key) == value, f"{preset_id}: {section}.{key} is {stored.get(key)!r}"


# ── deleting meetings by label ─────────────────────────────────────────────


def _pool_id(client) -> str:
    return client.vp.user_pool_id


def _email_domain(stack: Stack) -> str:
    """A domain the user pool accepts: the first of AllowedSignUpEmailDomain, else example.com.

    With AllowedSignUpEmailDomain set, the pool's pre-sign-up check refuses an
    admin-created user whose email is in any other domain.
    """
    parameters = stack.session.client("cloudformation").describe_stacks(StackName=stack.name)["Stacks"][0]
    allowed = next(
        (p.get("ParameterValue", "") for p in parameters.get("Parameters", [])
         if p["ParameterKey"] == "AllowedSignUpEmailDomain"),
        "",
    )
    domains = [domain.strip() for domain in allowed.split(",") if domain.strip()]
    return domains[0] if domains else "example.com"


@pytest.fixture(scope="module")
def users(client, stack: Stack):
    """A temporary user and a temporary Admin, deleted after.

    Created with no password and no invitation message (MessageAction SUPPRESS).
    """
    cognito = stack.session.client("cognito-idp")
    pool = _pool_id(client)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    domain = _email_domain(stack)
    made: dict[str, str] = {}
    try:
        for role in ("owner", "admin"):
            name = f"integ-hosting-{role}-{stamp}@{domain}"
            created = cognito.admin_create_user(
                UserPoolId=pool,
                Username=name,
                MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": name}],
            )
            made[role] = created["User"]["Username"]
        cognito.admin_add_user_to_group(UserPoolId=pool, Username=made["admin"], GroupName="Admin")
        yield made
    finally:
        for username in made.values():
            try:
                cognito.admin_delete_user(UserPoolId=pool, Username=username)
            except Exception as error:  # noqa: BLE001
                print(f"could not delete the temporary user {username}: {error}")


def _list_key(created_at: str, call_id: str) -> dict[str, Any]:
    """The key of a meeting's list entry, as createCall writes it (a four-hour shard of the day)."""
    shard = int(created_at[11:13]) // 4
    return {
        "PK": {"S": f"cls#{created_at[:10]}#s#{shard:02d}"},
        "SK": {"S": f"ts#{created_at}#id#{call_id}"},
    }


def _posix(name: str) -> str:
    """A file name as bedrock_summary_lambda posixify_filename makes it."""
    return re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", name)))


@pytest.fixture(scope="module")
def labelled(client, stack: Stack, users: dict[str, str]):
    """Synthetic meetings with a unique label, and the label; every one removed after."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    value = f"integ-{stamp}"
    kinesis = stack.session.client("kinesis")
    ddb = stack.session.client("dynamodb")
    s3 = stack.session.client("s3")
    stream = stack.outputs["CallDataStreamName"]
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    meetings = {
        # name: (owner, label value, ended)
        "own-1": (users["owner"], value, True),
        "own-2": (users["owner"], value, True),
        "other": (OTHER_OWNER, value, True),
        "unlabelled": (users["owner"], f"{value}-other", True),
        "live": (users["owner"], value, False),
    }
    call_ids = {name: f"integ-hosting-{name}-{stamp}" for name in meetings}
    created_at = {name: (base + timedelta(seconds=index)).isoformat() for index, name in enumerate(meetings)}
    files: list[str] = []
    try:
        for name, (owner, label, _ended) in meetings.items():
            call_id = call_ids[name]
            record = {
                "EventType": "START",
                "CallId": call_id,
                "CustomerPhoneNumber": "Integ Test Participant",
                "SystemPhoneNumber": "LMA Integ Test",
                "AgentId": owner,
                "CreatedAt": created_at[name],
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
                "Labels": [{"Key": "engagement", "Value": label}],
            }
            kinesis.put_record(StreamName=stream, Data=json.dumps(record).encode(), PartitionKey=call_id)
        for name, (owner, _label, ended) in meetings.items():
            call_id = call_ids[name]
            meeting = wait_for(lambda c=call_id: read_meeting(ddb, stack.event_table, c),
                               timeout=MEETING_RECORD_TIMEOUT_S, interval=3.0)
            assert meeting, f"the START for {call_id} did not create a meeting"
            if ended:
                # As END leaves it, without the summary END would start.
                ddb.update_item(
                    TableName=stack.event_table,
                    Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
                    UpdateExpression="SET #s = :ended",
                    ExpressionAttributeNames={"#s": "Status"},
                    ExpressionAttributeValues={":ended": {"S": "ENDED"}},
                )
        # Parts beyond the record, for the meeting deleted whole: a transcript
        # item, its transcript file and a prepared archive.
        whole = call_ids["own-1"]
        ddb.put_item(
            TableName=stack.event_table,
            Item={
                "PK": {"S": f"trs#{whole}"},
                "SK": {"S": "ts#2026-01-01T00:00:00Z#id#integ-segment"},
                "CallId": {"S": whole},
                "SegmentId": {"S": "integ-segment"},
                "Transcript": {"S": "A synthetic transcript line."},
                "Owner": {"S": users["owner"]},
            },
        )
        for key in (
            f"{TRANSCRIPTS_PREFIX}{_posix(whole)}-TRANSCRIPT.txt",
            f"{ARCHIVES_PREFIX}{_posix(whole)}/integ-archive.zip",
        ):
            s3.put_object(Bucket=stack.recordings_bucket, Key=key, Body=b"synthetic")
            files.append(key)
        yield {"label": value, "call_ids": call_ids, "created_at": created_at, "files": files}
    finally:
        for name, call_id in call_ids.items():
            cleanup(client.stack_name, call_id, region=client.region)
            # cleanup() removes the c#<CallId> partition only; the meeting's list
            # entry, which lma meetings delete reads, is removed here.
            try:
                ddb.delete_item(TableName=stack.event_table, Key=_list_key(created_at[name], call_id))
            except Exception:  # noqa: BLE001
                pass
            try:
                ddb.delete_item(
                    TableName=stack.event_table,
                    Key={"PK": {"S": f"trs#{call_id}"}, "SK": {"S": "ts#2026-01-01T00:00:00Z#id#integ-segment"}},
                )
            except Exception:  # noqa: BLE001
                pass
        for key in files:
            try:
                s3.delete_object(Bucket=stack.recordings_bucket, Key=key)
            except Exception:  # noqa: BLE001
                pass


def _lma(client, *args: str) -> tuple[int, str]:
    """``lma meetings delete ...`` run through the CLI itself, as an operator runs it."""
    # The stack and Region given to the command itself, which nothing in the
    # environment overrides.
    result = CliRunner().invoke(
        main,
        ["meetings", "delete", "--stack-name", client.stack_name,
         *(["--region", client.region] if client.region else []), *args],
    )
    return result.exit_code, result.output


def _json(client, *args: str) -> dict[str, Any]:
    code, output = _lma(client, *args, "--json")
    assert code in (0, 1), output
    return json.loads(output)


def _exists(stack: Stack, call_id: str) -> bool:
    return read_meeting(stack.session.client("dynamodb"), stack.event_table, call_id) is not None


def test_a_dry_run_lists_a_users_own_labelled_meetings_and_deletes_nothing(client, stack, users, labelled) -> None:
    ids = labelled["call_ids"]
    result = _json(client, "--label", f"engagement={labelled['label']}", "--user-name", users["owner"])
    assert result["dry_run"] is True and result["scope"] == "own"
    assert sorted(m["call_id"] for m in result["matched"]) == sorted([ids["own-1"], ids["own-2"]])
    assert [m["call_id"] for m in result["skipped_in_progress"]] == [ids["live"]]
    assert result["deleted"] == []
    assert all(_exists(stack, call_id) for call_id in ids.values())


def test_an_admins_dry_run_lists_every_users_labelled_meetings(client, stack, users, labelled) -> None:
    ids = labelled["call_ids"]
    result = _json(client, "--label", f"engagement={labelled['label']}", "--user-name", users["admin"])
    assert result["scope"] == "all"
    assert sorted(m["call_id"] for m in result["matched"]) == sorted([ids["own-1"], ids["own-2"], ids["other"]])
    assert all(_exists(stack, call_id) for call_id in ids.values())


def test_the_owner_deletes_their_labelled_meetings_whole(client, stack, users, labelled) -> None:
    ids = labelled["call_ids"]
    code, output = _lma(client, "--label", f"engagement={labelled['label']}", "--user-name", users["owner"],
                        "--confirm")
    assert code == 0, output
    assert "Deleted 2 of 2 meeting(s)." in output
    ddb = stack.session.client("dynamodb")
    for name in ("own-1", "own-2"):
        assert not _exists(stack, ids[name]), f"{name} was not deleted"
    # The parts beyond the record.
    items = ddb.query(
        TableName=stack.event_table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"trs#{ids['own-1']}"}},
    )["Items"]
    assert items == [], "the meeting's transcript items were not deleted"
    s3 = stack.session.client("s3")
    for key in labelled["files"]:
        versions = s3.list_object_versions(Bucket=stack.recordings_bucket, Prefix=key)
        assert not versions.get("Versions") and not versions.get("DeleteMarkers"), f"{key} was not deleted"
    # The list entry, at the keys createCall gives it.
    listed = ddb.get_item(
        TableName=stack.event_table, Key=_list_key(labelled["created_at"]["own-1"], ids["own-1"])
    ).get("Item")
    assert listed is None, "the meeting's list entry was not deleted"
    # Not the owner's, another label, or still in progress: kept.
    for name in ("other", "unlabelled", "live"):
        assert _exists(stack, ids[name]), f"{name} was deleted"


def test_an_admin_deletes_another_users_labelled_meeting(client, stack, users, labelled) -> None:
    ids = labelled["call_ids"]
    code, output = _lma(client, "--label", f"engagement={labelled['label']}", "--user-name", users["admin"],
                        "--confirm")
    assert code == 0, output
    assert not _exists(stack, ids["other"])
    assert "still in progress" in output
    assert _exists(stack, ids["live"]) and _exists(stack, ids["unlabelled"])


# ── who may ask (opt-in) ───────────────────────────────────────────────────

ASSISTANT_ENABLED = os.environ.get("LMA_TEST_HOSTING_ASSISTANT", "") not in ("", "0", "false")


@pytest.mark.optional_feature
@pytest.mark.skipif(not ASSISTANT_ENABLED, reason="opt-in: set LMA_TEST_HOSTING_ASSISTANT=1")
def test_requests_made_in_the_meeting_are_refused_while_who_may_ask_is_off(stack: Stack) -> None:
    from assistant_profile_probe import APPLY_POLL_SECONDS, APPLY_TIMEOUT_SECONDS, invoke_assistant
    from meeting_consent_probe import ConsentSettingsOverride

    refusal = "Requests made in the meeting are turned off on this deployment"
    override = ConsentSettingsOverride(stack, section="meeting_assist", updated_by="integ-tests:hosting-presets")
    override.save()
    try:
        override.apply(inMeetingRequesters="off")
        deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
        answer: dict[str, Any] = {}
        while time.monotonic() < deadline:
            answer = invoke_assistant(stack.session, stack.name, "What is two plus two?", tool_scope="meeting_chat")
            assert not answer["function_error"], answer
            if answer["message"].startswith(refusal):
                break
            time.sleep(APPLY_POLL_SECONDS)
        assert answer.get("message", "").startswith(refusal), answer
        page = invoke_assistant(stack.session, stack.name, "What is two plus two?",
                                user_email="integ-tests@example.com")
        assert not page["function_error"], page
        assert page["message"] and not page["message"].startswith(refusal), page
    finally:
        override.restore()
