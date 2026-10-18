# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Drive the voice-enrollment consent lifecycle against a deployed stack.

Five rounds of code review cannot establish that a consent row actually lands
in DynamoDB, that a MicroVM really computes a voiceprint from real audio, or
that a withdrawal really destroys it. This does, through the same AppSync
mutations the portal uses, and then reads the tables directly to check what was
stored rather than what the API said.

The assertions are about the **records**, because the records are what the
consent terms promise. Specifically:

* no voiceprint exists before consent is recorded;
* the stored enrollment holds a centroid and the hash of the terms agreed to,
  and no audio;
* no consent-log row holds biometric data, and none records the hash of an
  empty string -- which reads as "the terms shown were empty" rather than "not
  recorded";
* a withdrawal destroys the centroid, sets ``revoked``, and appends a row;
* and the terms are offered again afterwards, so withdrawing is reversible as
  the terms say it is.

Self-cleaning: it withdraws at the end, which deletes the voiceprint it
created. Safe to repeat -- it appends to an append-only log by design.
"""

from __future__ import annotations

import base64
import hashlib
import json

import boto3
import requests

# What the portal asks somebody to read. Phonetically varied on purpose: a
# voiceprint from one short phrase recognises the phrase more than the person.
SCRIPT = [
    "The quick brown fox jumps over the lazy dog, and the early bird catches the worm.",
    "I would like a large coffee, two slices of toast, and the morning paper, please.",
    "Thursday afternoon was unusually warm, so we walked along the river to the bridge.",
    "Seventeen, forty-two, ninety-eight, three hundred and six, one thousand and five.",
    "My train leaves from platform nine at quarter past six, which gives us just enough time.",
    "She bought eight yellow jugs, a huge rug, and a thick woollen blanket at the market.",
]

# 16 kHz 16-bit mono is what the backend reads.
SAMPLE_RATE = 16000

TERMS_QUERY = """
query Terms {
  voiceEnrollmentTerms {
    Success Message Status DisplayName ConsentExpiresAt NeedsReconsent TermsVersion Terms
  }
}
"""
GRANT = "mutation Grant { grantVoiceConsent { Success Message Status } }"
ENROL = """
mutation Enrol($input: EnrolMyVoiceInput!) {
  enrolMyVoice(input: $input) { Success Message Status }
}
"""
REVOKE = """
mutation Revoke($input: RevokeVoiceConsentInput) {
  revokeVoiceConsent(input: $input) { Success Message Status }
}
"""

EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def _graphql_url(outputs):
    return [
        part.split("=", 1)[1]
        for part in outputs["LocalUITestingEnv"].split()
        if part.startswith("VITE_APPSYNC_GRAPHQL_URL=")
    ][0]


def _tokens(outputs, username, password, region):
    from pycognito.aws_srp import AWSSRP

    pool_id = outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    srp = AWSSRP(
        username=username,
        password=password,
        pool_id=pool_id,
        client_id=outputs["CognitoUserPoolClientId"],
        client=boto3.client("cognito-idp", region_name=region),
    )
    result = srp.authenticate_user()["AuthenticationResult"]
    return result["IdToken"], result["AccessToken"]


def _call(url, token, query, variables=None):
    response = requests.post(
        url,
        headers={"Authorization": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL error: {json.dumps(body['errors'])[:400]}")
    return body["data"]


def _voice_tables(stack_name, region):
    """The two voice-enrollment tables, or None when the feature is off.

    Returning None rather than raising lets the caller skip cleanly: with
    EnableVoiceEnrollment=false these resources do not exist at all, which is
    the default and not a failure.
    """
    cfn = boto3.client("cloudformation", region_name=region)
    found = {}
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        for resource in page["StackResourceSummaries"]:
            if resource["ResourceType"] != "AWS::CloudFormation::Stack":
                continue
            if "AISTACK" not in resource["LogicalResourceId"].upper():
                continue
            inner = resource["PhysicalResourceId"].split("/")[1]
            for sub in cfn.get_paginator("list_stack_resources").paginate(StackName=inner):
                for item in sub["StackResourceSummaries"]:
                    if item["ResourceType"] != "AWS::DynamoDB::Table":
                        continue
                    if "VoiceEnrollmentTable" in item["LogicalResourceId"]:
                        found["enrollments"] = item["PhysicalResourceId"]
                    elif "VoiceConsentLogTable" in item["LogicalResourceId"]:
                        found["consent_log"] = item["PhysicalResourceId"]
    if "enrollments" in found and "consent_log" in found:
        return found
    return None


def _synthesise(region):
    """The script as raw PCM clips, via Amazon Polly.

    Synthesised rather than recorded so the probe needs no audio fixture in the
    repository, and so the same voice can later be matched in a meeting.
    """
    polly = boto3.client("polly", region_name=region)
    clips = []
    for line in SCRIPT:
        clips.append(
            polly.synthesize_speech(
                Text=line,
                OutputFormat="pcm",
                VoiceId="Joanna",
                SampleRate=str(SAMPLE_RATE),
                Engine="neural",
            )["AudioStream"].read()
        )
    return clips


def run_probe(stack_name, username, password, region=None, display_name="lma-integ-test"):
    """Walk the lifecycle. Returns what was observed; raises only on a bad call."""
    cfn = boto3.client("cloudformation", region_name=region)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    }
    tables = _voice_tables(stack_name, region)
    if tables is None:
        return {"enabled": False}

    url = _graphql_url(outputs)
    id_token, access_token = _tokens(outputs, username, password, region)
    subject = next(
        attribute["Value"]
        for attribute in boto3.client("cognito-idp", region_name=region).get_user(
            AccessToken=access_token
        )["UserAttributes"]
        if attribute["Name"] == "sub"
    )
    ddb = boto3.client("dynamodb", region_name=region)

    def enrollment():
        return (
            ddb.get_item(TableName=tables["enrollments"], Key={"SubjectId": {"S": subject}}).get(
                "Item"
            )
            or {}
        )

    def consent_log():
        return ddb.query(
            TableName=tables["consent_log"],
            KeyConditionExpression="SubjectId = :s",
            ExpressionAttributeValues={":s": {"S": subject}},
        )["Items"]

    observed = {"enabled": True, "subject": subject}

    state = _call(url, id_token, TERMS_QUERY)["voiceEnrollmentTerms"]
    observed["terms_length"] = len(state.get("Terms") or "")

    granted = _call(url, id_token, GRANT)["grantVoiceConsent"]
    observed["granted"] = bool(granted["Success"])
    observed["grant_message"] = granted["Message"]
    observed["voiceprint_before_audio"] = "Centroid" in enrollment()

    clips = _synthesise(region)
    seconds = sum(len(clip) / 2 / SAMPLE_RATE for clip in clips)
    observed["clips"] = len(clips)
    observed["seconds"] = round(seconds, 1)

    enrolled = _call(
        url,
        id_token,
        ENROL,
        {
            "input": {
                "DisplayName": display_name,
                "Samples": [base64.b64encode(clip).decode() for clip in clips],
                "TotalSeconds": round(seconds, 2),
            }
        },
    )["enrolMyVoice"]
    observed["enrolled"] = bool(enrolled["Success"])
    observed["enrol_message"] = enrolled["Message"]

    stored = enrollment()
    observed["status_after_enrol"] = stored.get("Status", {}).get("S")
    observed["centroid_dimensions"] = len(stored.get("Centroid", {}).get("L") or [])
    observed["enrollment_pins_terms"] = bool(stored.get("TermsSha256", {}).get("S"))
    observed["audio_on_enrollment"] = any(
        key in stored for key in ("Samples", "Audio", "Recordings")
    )

    rows = consent_log()
    observed["actions"] = [row["Action"]["S"] for row in rows]
    observed["biometric_in_log"] = any("Centroid" in row for row in rows)
    observed["empty_hash_in_log"] = any(
        row.get("TermsSha256", {}).get("S") == EMPTY_HASH for row in rows
    )

    revoked = _call(url, id_token, REVOKE, {"input": {}})["revokeVoiceConsent"]
    observed["revoked"] = bool(revoked["Success"])
    after = enrollment()
    observed["centroid_after_revoke"] = "Centroid" in after
    observed["status_after_revoke"] = after.get("Status", {}).get("S")
    observed["actions_after_revoke"] = [row["Action"]["S"] for row in consent_log()]

    final = _call(url, id_token, TERMS_QUERY)["voiceEnrollmentTerms"]
    observed["terms_offered_again"] = bool(final.get("Terms"))
    return observed
