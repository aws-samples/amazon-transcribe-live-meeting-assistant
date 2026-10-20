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


# Trailing voice id the transcriber appends when speaker separation is on, e.g.
# 'Other Participant (spk_0)'. Mirrors splitDiarizationLabel in the UI.
DIARIZATION_LABEL = re.compile(r"^(.*?)(\s*\(spk_\d+\))$")

# The placeholder the transcriber writes for the remote side of a streaming
# meeting before anyone has been named.
DEFAULT_OTHER_SPEAKER_NAME = "Other Participant"


def split_diarization_label(speaker):
    """Split a stored speaker into its base name and its voice id."""
    match = DIARIZATION_LABEL.match(speaker or "")
    return (match.group(1), match.group(2)) if match else (speaker or "", "")


def parse_speaker_identities(raw):
    """Read the meeting's speaker identity map, tolerating anything.

    Python twin of ui/src/components/common/speaker-identities.js. It has to exist
    separately because this function is the single chokepoint for every LLM-facing
    transcript -- the summary prompts, the knowledge-base export and the meeting
    assistant all come through here. If names were resolved only in the browser,
    the UI would show 'Bob' while every summary still said 'spk_0'.

    A meeting written before this feature has no attribute, and a blob from a newer
    version may carry unknown keys. Both must yield a transcript rather than an
    error, so every failure returns the empty map.
    """
    empty = {"identities": {}, "merges": {}, "segmentOverrides": {}}
    if not raw:
        return empty
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        logger.warning("Ignoring unparseable SpeakerIdentities")
        return empty
    if not isinstance(parsed, dict):
        return empty
    return {
        key: parsed[key] if isinstance(parsed.get(key), dict) else {}
        for key in ("identities", "merges", "segmentOverrides")
    }


def speaker_key(channel, speaker):
    """Key for one voice: its channel plus the speaker string AS STORED.

    Voice ids are numbered per channel and restart at spk_0, so including the
    stored base name is what keeps 'Alice (spk_0)' and 'Bob (spk_0)' apart.
    """
    return f"{channel or ''}#{(speaker or '').strip()}"


def resolve_merge_target(key, merges):
    """Follow a merge chain to the key that holds the name, stopping on a cycle."""
    current = key
    seen = {current}
    while merges.get(current) and merges[current] not in seen:
        current = merges[current]
        seen.add(current)
    return current


def resolve_speaker_name(row, identities, fallback_name):
    """Display name for one segment, applying the meeting's identity map.

    Must agree with resolveSpeaker in the UI's speaker-identities.js for the same
    inputs, including the fallback rules -- a divergence means the transcript on
    screen and the transcript the summary prompts read disagree about who spoke.
    """
    override = identities["segmentOverrides"].get(row.get("SegmentId"))
    if override:
        return override

    stored = row.get("Speaker") or ""
    channel = row.get("Channel")
    target = resolve_merge_target(speaker_key(channel, stored), identities["merges"])
    entry = identities["identities"].get(target)
    if isinstance(entry, dict) and entry.get("name"):
        return entry["name"]

    # No mapping. Keep the voice id: two voices on one channel differ only by it,
    # so dropping it would read as one person in every summary.
    base, suffix = split_diarization_label(stored)
    # The participant name from the Stream Audio form is the REMOTE side's name,
    # so it substitutes only on the caller channel; applying it to the local
    # microphone channel would label that channel with the wrong person.
    if base == DEFAULT_OTHER_SPEAKER_NAME and channel == "CALLER" and fallback_name:
        return f"{fallback_name}{suffix}"
    # A segment with no recorded speaker stays unlabelled, which is this
    # function's long-standing contract: better an unattributed line than an
    # invented one.
    #
    # This is the one place the UI resolver deliberately differs. There, an empty
    # stored speaker takes the same branch as the placeholder, so a caller-channel
    # segment renders under the Stream Audio participant name (and otherwise under
    # the generic placeholder) -- a transcript row has to put something in its
    # speaker column, whereas a prompt line reads better with no attribution than
    # with a guessed one.
    return stored


def preprocess_transcripts(transcripts, condense, includeSpeaker, metadata=None):
    data = []
    call_metadata = metadata or {}
    identities = parse_speaker_identities(call_metadata.get("SpeakerIdentities"))
    # The name the user typed for the remote side on the Stream Audio form. The UI
    # has always substituted it for the placeholder; doing it here too is what
    # stops a summary naming someone the transcript on screen does not.
    fallback_name = call_metadata.get("CustomerPhoneNumber")
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
                speakerName = resolve_speaker_name(row, identities, fallback_name)
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

    There is also no token pressure for it to relieve: nothing trims the
    transcript on the way in, and the model's maxTokens bounds the output, not
    the input. If transcript quality needs improving, that belongs in the ASR
    configuration, not in a regex over its output.
    """
    return re.sub(html_remover, "", transcript_string)


def lambda_handler(event, context):
    print("Received event: " + json.dumps(event, indent=2))

    # Setup model input data using text (utterances) received from LCA
    data = json.loads(json.dumps(event))
    callid = data["CallId"]

    preProcess = False
    if "ProcessTranscript" in data:
        preProcess = data["ProcessTranscript"]

    includeSpeaker = False
    if "IncludeSpeaker" in data:
        includeSpeaker = data["IncludeSpeaker"]

    # Read the meeting row before assembling the text: it carries the speaker
    # identity map that turns a stored voice id into a name.
    metadata = get_call_metadata(callid)
    transcripts = get_transcripts(callid)
    transcripts = preprocess_transcripts(transcripts, preProcess, includeSpeaker, metadata)
    transcript_string = "".join(transcripts)
    response = {"transcript": transcript_string, "metadata": metadata}
    # Log the shape of the response rather than the response. Everything this
    # function returns is meeting content, and the counts are what is actually
    # useful when diagnosing a short or empty transcript.
    print(
        f"Fetch Transcript response: {len(transcript_string)} characters "
        f"over {len(transcripts)} segment(s)"
    )
    return response


# Test case
if __name__ == "__main__":
    lambda_handler(
        {
            "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
            "ProcessTranscript": False,
            "LastNTurns": 20,
        },
        {},
    )
