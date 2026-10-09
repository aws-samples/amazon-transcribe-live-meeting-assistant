# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import json
import logging
import os
import re

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

# grab environment variables
LCA_CALL_EVENTS_TABLE = os.environ["LCA_CALL_EVENTS_TABLE"]

runtime = boto3.client("runtime.sagemaker")
logger = logging.getLogger(__name__)

issue_remover = re.compile("<span class='issue-pill'>Issue Detected</span>")
html_remover = re.compile("<[^>]*>")

ddb = boto3.resource("dynamodb")
ddbTable = ddb.Table(LCA_CALL_EVENTS_TABLE)


# Upper bound on Query pages for one meeting's transcript. A 1 MB page holds a
# great many segments, so this is far above any real meeting; it exists so that a
# table which keeps handing back a continuation key cannot spin this Lambda for
# its whole timeout.
MAX_TRANSCRIPT_PAGES = 100


def get_call_metadata(callid):
    pk = "c#" + callid
    print(f"Call metadata PK: {pk}")
    try:
        metadata = ddbTable.get_item(Key={"PK": pk, "SK": pk}, TableName=LCA_CALL_EVENTS_TABLE)
    except ClientError as err:
        logger.error(
            "Error getting metadata from LCA Call Events table %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return metadata["Item"]


def get_transcripts(callid):
    """Every final transcript segment for a meeting, across all result pages.

    DynamoDB caps a Query response at 1 MB of items *read*, and applies
    FilterExpression only after that cap. Partial segments are filtered here and
    are numerous, so they consume the budget without appearing in the result --
    which means even a moderately long meeting is spread over several pages. A
    single query returns a silently truncated transcript, and the summary, the
    knowledge base export and the assistant then all describe a partial meeting
    as though it were the whole one. So follow LastEvaluatedKey to the end.
    """
    pk = "trs#" + callid
    print(f"Call transcripts PK: {pk}")
    items = []
    start_key = None
    pages = 0
    while pages < MAX_TRANSCRIPT_PAGES:
        query_args = {
            "KeyConditionExpression": Key("PK").eq(pk),
            "FilterExpression": (Attr("Channel").eq("AGENT") | Attr("Channel").eq("CALLER"))
            & Attr("IsPartial").eq(False),
        }
        if start_key:
            query_args["ExclusiveStartKey"] = start_key
        try:
            response = ddbTable.query(**query_args)
        except ClientError as err:
            logger.error(
                "Error getting transcripts from LCA Call Events table %s: %s",
                err.response["Error"]["Code"],
                err.response["Error"]["Message"],
            )
            raise
        items.extend(response.get("Items", []))
        pages += 1
        next_key = response.get("LastEvaluatedKey")
        if not next_key or next_key == start_key:
            # A key identical to the one just used would ask for the same page
            # again forever. Stop rather than spin.
            break
        start_key = next_key
    if pages >= MAX_TRANSCRIPT_PAGES:
        logger.warning(
            "Stopped reading transcript for %s at the %d page limit; "
            "the transcript may be incomplete",
            callid,
            MAX_TRANSCRIPT_PAGES,
        )
    print(f"Read {len(items)} final segments for {callid} over {pages} page(s)")
    return items


def preprocess_transcripts(transcripts, condense, includeSpeaker):
    data = []
    transcripts.sort(key=lambda x: x["EndTime"])
    for row in transcripts:
        transcript = row["Transcript"]
        # prefix Speaker name to transcript segments if "IncludeSpeaker" parameter is set to True.
        if includeSpeaker:
            # For LMA 'OK Assistant' answers, we should keep assistant replies as part of the transcript for any contextual followup 'OK Assistant' questions.
            if row["Channel"] == "AGENT_ASSISTANT":
                # Add the 'MeetingAssistant:' prefix for assistant messages
                transcript = "MeetingAssistant: " + transcript
            else:
                # Add the 'Speaker:' prefix for Transcript segments if "Speaker" field is present
                speakerName = row.get("Speaker", None)
                if speakerName:
                    transcript = speakerName.strip() + ": " + transcript

        if condense:
            # Strips UI markup only. Filler words are deliberately left in: see
            # the note on remove_html below.
            transcript = remove_issues(transcript)
            transcript = remove_html(transcript).strip()
            if len(transcript) > 1:
                transcript = "\n" + transcript
        else:
            transcript = "\n" + transcript
        data.append(transcript)
    return data


def remove_issues(transcript_string):
    return re.sub(issue_remover, "", transcript_string)


def remove_html(transcript_string):
    """Strip the HTML the web UI renders, which is noise in a prompt.

    Condensing removes *markup*, not speech. A filler-word pass used to run here
    too, deleting "um", "uh", "mhm" and "like" -- but the models these transcripts
    are summarised by are untroubled by disfluent speech, so the pass bought
    nothing a model cared about while corrupting real words: the pattern had no
    trailing word boundary, so "umbrella" became "brella" and "Likewise" became
    "wise". Worse, most of its saving came from deleting every "like", including
    the meaning-bearing ones -- "looks like it needs sign-off" lost its verb.

    There is also no token pressure for it to relieve: TOKEN_COUNT defaults to 0
    (no truncation) and the model's maxTokens bounds the output, not the input.
    If transcript quality needs improving, that belongs in the ASR
    configuration, not in a regex over its output.
    """
    return re.sub(html_remover, "", transcript_string)


def truncate_number_of_words(transcript_string, truncateLength):
    # findall can retain carriage returns
    data = re.findall(r"\S+|\n|.|,", transcript_string)
    if truncateLength > 0:
        data = data[0:truncateLength]
    print("Token Count: " + str(len(data)))
    return "".join(data)


def lambda_handler(event, context):
    print("Received event: " + json.dumps(event, indent=2))

    # Setup model input data using text (utterances) received from LCA
    data = json.loads(json.dumps(event))
    callid = data["CallId"]
    tokenCount = 0
    if "TokenCount" in data:
        tokenCount = data["TokenCount"]

    preProcess = False
    if "ProcessTranscript" in data:
        preProcess = data["ProcessTranscript"]

    includeSpeaker = False
    if "IncludeSpeaker" in data:
        includeSpeaker = data["IncludeSpeaker"]

    transcripts = get_transcripts(callid)
    transcripts = preprocess_transcripts(transcripts, preProcess, includeSpeaker)
    transcript_string = "".join(transcripts)
    transcript_string = truncate_number_of_words(transcript_string, tokenCount)
    metadata = get_call_metadata(callid)
    response = {"transcript": transcript_string, "metadata": metadata}
    print("Fetch Transcript response:", response)
    return response


# Test case
if __name__ == "__main__":
    lambda_handler(
        {
            "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
            "TokenCount": 0,
            "ProcessTranscript": False,
            "LastNTurns": 20,
        },
        {},
    )
