# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
# Invokes Anthropic generate text API using requests module
# see https://console.anthropic.com/docs/api/reference for more details

import json
import logging
import os
import re

import boto3
from bedrock_compat import SUMMARY_MAX_TOKENS, build_inference_config, extract_text
from botocore.config import Config

# pylint: disable=import-error
from lma_config import SUMMARY_PROMPT_TEMPLATES, ConfigurationStore  # configuration layer
from lma_metering import MeteringWriter, bedrock_service, bedrock_usage  # configuration layer

# pylint: enable=import-error

# The root logger's level is what the configuration layer's own loggers inherit,
# so this is also the level at which its warnings (an unreadable profile, a
# fallback to the baseline) reach the function's log group.
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# grab environment variables

# use inference profile for model id as Nova models require the use of inference profiles
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
FETCH_TRANSCRIPT_LAMBDA_ARN = os.environ["FETCH_TRANSCRIPT_LAMBDA_ARN"]
PROCESS_TRANSCRIPT = os.getenv("PROCESS_TRANSCRIPT", "False") == "True"
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
S3_PREFIX = os.environ["S3_PREFIX"]

# A prompt template is stored under a key of the form "N#Title", where N orders
# the sections of the summary. Kept identical to the pattern that the
# update_llm_prompt_template_resolver applies on write, so the reader and the
# writer agree on which keys of the section are prompts.
#
# Everything in the section is a prompt: the record's provenance -- who saved it
# and when -- lives in the stored item's metadata attributes, which the
# configuration accessor keeps out of the body. So a key in any other form can
# only have been added by hand in the DynamoDB console, and each one gets a line
# in the log naming it, because the person who added it is the one who needs to
# know it is not being used as a prompt.
PROMPT_KEY_PATTERN = re.compile(r"^\d+#")

# A prompt that names the meeting's agenda. It is run only for a meeting that has
# one: the shipped "AGENDA COVERAGE" section reports progress against the agenda,
# and for a meeting without one there is nothing to report, so the section is
# left out of the summary rather than generated empty. See docs/meeting-agenda.md.
AGENDA_PLACEHOLDER = "{agenda}"
PLACEHOLDERS = re.compile(r"\{transcript\}|\{agenda\}")

# List marks at the start of an agenda line, hidden as the meeting page hides
# them (ui/src/components/common/agenda.js), so the prompt numbers the items the
# way the page does.
AGENDA_LIST_MARK = re.compile(r"^\s*(?:[-*\u2022]|\d{1,3}[.)]|[a-zA-Z][.)])\s+")

# Optional environment variables allow region / endpoint override for bedrock Boto3
BEDROCK_REGION = (
    os.environ["BEDROCK_REGION_OVERRIDE"]
    if "BEDROCK_REGION_OVERRIDE" in os.environ
    else os.environ["AWS_REGION"]
)
BEDROCK_ENDPOINT_URL = os.environ.get(
    "BEDROCK_ENDPOINT_URL", f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
)


lambda_client = boto3.client("lambda")

# Built at module scope, not per invocation: the section's declared cache tier
# only means anything if the store outlives a single call. A summary reads the
# prompts once, so a container handling several meetings in a row reads DynamoDB
# once per five minutes instead of twice per summary.
CONFIG_STORE = ConfigurationStore()

# Token usage of each model call, recorded against the meeting for its cost
# estimate. Inert when METERING_TABLE_NAME is unset.
METERING = MeteringWriter(source="bedrock-summary")

bedrock = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION,
    endpoint_url=BEDROCK_ENDPOINT_URL,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


def get_templates_from_dynamodb(prompt_override, refresh_configuration=False):
    templates = []
    prompt_template_str = None

    if prompt_override is not None:
        print("Prompt Template String override:", prompt_override)
        prompt_template_str = prompt_override
        try:
            prompt_templates = json.loads(prompt_template_str)
            for k, v in prompt_templates.items():
                prompt = v.replace("<br>", "\n")
                templates.append({k: prompt})
        except Exception:
            prompt = prompt_template_str.replace("<br>", "\n")
            templates.append({"Summary": prompt})

    if prompt_template_str is None:
        try:
            if refresh_configuration:
                # Somebody pressed Regenerate, and the Meeting Summary panel
                # promises that doing so uses the prompt templates as they are
                # now. The section's cache is right for the end-of-meeting runs
                # this function does unattended and wrong for a button an
                # administrator pressed straight after saving a prompt, so this
                # path drops the cached copy and reads again.
                CONFIG_STORE.invalidate()
            # One read of the shared configuration table, which resolves the
            # deployment baseline with the active profile's edits merged over it.
            # The section declares the standard cache tier, so the read is served
            # from the store's cache for up to five minutes; the fingerprint in
            # the line below is what tells one refetch from another in the log.
            section = CONFIG_STORE.get_section(SUMMARY_PROMPT_TEMPLATES.name)
            print(
                f"Summary prompt templates: {len(section.values)} key(s), "
                f"profile={section.active_profile or 'none'}, "
                f"fingerprint={section.fingerprint}, cached={section.served_from_cache}"
            )

            # An empty section means the table has not been seeded, or the
            # record was deleted. Raised rather than returning no prompts: a
            # summary generated from nothing is stored and shown as if it were a
            # real one, where an error is visible and the deployment is fixable.
            if not section.values:
                raise RuntimeError(
                    f"the configuration section "
                    f"{SUMMARY_PROMPT_TEMPLATES.name!r} holds nothing in "
                    f"{CONFIG_STORE.table_name}; the stack's configuration "
                    "seeder has not run or its records were removed"
                )

            # A prompt key is "N#Title" -- the form the Transcript Summary page
            # writes, and the one the sequence-number stripping below assumes.
            for k in sorted(section.values):
                if PROMPT_KEY_PATTERN.match(k):
                    prompt = section.values[k]
                    if not isinstance(prompt, str):
                        # Only a hand edit can store one; reported rather than
                        # dropped, since whoever made it needs to know.
                        print(
                            f"Not using '{k}' as a summary prompt: a prompt must be "
                            f"text, and this one is {type(prompt).__name__}."
                        )
                        continue
                    # skip if prompt value is empty, or set to 'NONE'
                    if prompt and prompt != "NONE":
                        prompt = prompt.replace("<br>", "\n")
                        index = k.find("#")
                        k_stripped = k[index + 1 :]
                        templates.append({k_stripped: prompt})
                else:
                    print(
                        f"Not using '{k}' as a summary prompt: a prompt is stored "
                        "under a key of the form 'N#Title', where N is a sequence "
                        "number that orders the sections of the summary."
                    )
        except Exception as e:
            print("Exception:", e)
            raise (e)

    return templates


def get_transcripts(callId):
    payload = {
        "CallId": callId,
        "ProcessTranscript": PROCESS_TRANSCRIPT,
        "IncludeSpeaker": True,
    }
    print("Invoking lambda", payload)
    response = lambda_client.invoke(
        FunctionName=FETCH_TRANSCRIPT_LAMBDA_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    print("Lambda response:", response)
    transcript_data = response["Payload"].read().decode()
    transcript_json = json.loads(transcript_data)
    print("Transcript JSON:", transcript_json)
    return transcript_json


def get_generated_text(response):
    """The summary text, with any reasoning block the model emitted left out."""
    return extract_text(response)


def call_bedrock(prompt_data, call_id=None):
    modelId = BEDROCK_MODEL_ID
    print("Bedrock request - ModelId", modelId)
    message = {"role": "user", "content": [{"text": prompt_data}]}

    response = bedrock.converse(
        modelId=modelId,
        messages=[message],
        inferenceConfig=build_inference_config(modelId, SUMMARY_MAX_TOKENS),
    )
    # One record per section of the summary, so a regeneration -- which runs
    # every section again -- shows up as the extra usage it is.
    METERING.record(
        call_id,
        "Summary",
        bedrock_service(modelId),
        bedrock_usage(response.get("usage")),
        region=BEDROCK_REGION if BEDROCK_REGION != os.environ.get("AWS_REGION") else None,
    )
    generated_text = get_generated_text(response)
    print("Bedrock response: ", json.dumps(generated_text))
    return generated_text


def numbered_agenda(agenda):
    """The meeting's agenda as numbered lines, or "" when it has none."""
    items = []
    # Line breaks only, as the page splits (/\r?\n/), not every separator
    # str.splitlines() knows, so the section numbers items as the page does.
    for line in re.split(r"\r?\n", agenda) if isinstance(agenda, str) else []:
        item = AGENDA_LIST_MARK.sub("", line).strip()
        if item:
            items.append(item)
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, start=1))


def generate_summary(
    transcript, prompt_override, refresh_configuration=False, call_id=None, agenda=None
):
    # first check to see if this is one prompt, or many prompts as a json
    templates = get_templates_from_dynamodb(prompt_override, refresh_configuration)
    agenda_text = numbered_agenda(agenda)
    result = {}
    for item in templates:
        key = list(item.keys())[0]
        prompt = item[key]
        if AGENDA_PLACEHOLDER in prompt:
            if not agenda_text:
                print(f"Skipping summary section '{key}': the meeting has no agenda.")
                continue
        # Both placeholders in one pass, so text inserted for one -- an agenda
        # line that reads "{transcript}", say -- is never read as the other.
        values = {"{transcript}": transcript, AGENDA_PLACEHOLDER: agenda_text}
        prompt = PLACEHOLDERS.sub(lambda match: values[match.group(0)], prompt)
        print("Prompt:", prompt)
        response = call_bedrock(prompt, call_id)
        print("API Response:", response)
        result[key] = response
    if len(result.keys()) == 1:
        # there's only one summary in here, so let's return just that.
        # this may contain json or a string.
        return result[list(result.keys())[0]]
    return json.dumps(result)


def posixify_filename(filename: str) -> str:
    # Replace all invalid characters with underscores
    regex = r"[^a-zA-Z0-9_.]"
    posix_filename = re.sub(regex, "_", filename)
    # Remove leading and trailing underscores
    posix_filename = re.sub(r"^_+", "", posix_filename)
    posix_filename = re.sub(r"_+$", "", posix_filename)
    return posix_filename


def getKBMetadata(metadata):
    # Keys to include
    keys_to_include = [
        "CallId",
        "CreatedAt",
        "UpdatedAt",
        "Owner",
        "TotalConversationDurationMillis",
    ]
    # Create a new dictionary with only the specified keys
    filtered_metadata = {key: metadata[key] for key in keys_to_include if key in metadata}
    kbMetadata = {"metadataAttributes": filtered_metadata}
    return json.dumps(kbMetadata)


def format_summary(summary, metadata):
    summary_dict = json.loads(summary)
    summary_dict["MEETING NAME"] = metadata["CallId"]
    summary_dict["MEETING DATE AND TIME"] = metadata["CreatedAt"]
    summary_dict["MEETING DURATION (SECONDS)"] = int(
        metadata["TotalConversationDurationMillis"] / 1000
    )
    return json.dumps(summary_dict)


def write_to_s3(callId, metadata, transcript, summary):
    s3 = boto3.client("s3")
    filename = posixify_filename(f"{callId}")
    summary_file_key = f"{S3_PREFIX}{filename}-SUMMARY.txt"
    transcript_file_key = f"{S3_PREFIX}{filename}-TRANSCRIPT.txt"
    summary = format_summary(summary, metadata)
    kbMetadata = getKBMetadata(metadata)
    print(f"KB Summary: {summary}")
    print(f"KB Metadata: {kbMetadata}")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=summary_file_key, Body=summary)
    print(f"Wrote summary to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=f"{summary_file_key}.metadata.json", Body=kbMetadata)
    print(f"Wrote summary metadata to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}.metadata.json")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=transcript_file_key, Body=transcript)
    print(f"Wrote transcript to S3: s3://{S3_BUCKET_NAME}/{transcript_file_key}")
    s3.put_object(
        Bucket=S3_BUCKET_NAME, Key=f"{transcript_file_key}.metadata.json", Body=kbMetadata
    )
    print(
        f"Wrote transcript metadata to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}.metadata.json"
    )


def handler(event, context):
    print("Received event: ", json.dumps(event))
    callId = event["CallId"]
    try:
        transcript_json = get_transcripts(callId)
        transcript = transcript_json["transcript"]
        metadata = transcript_json["metadata"]
        summary = "No summary available"
        prompt_override = None
        if "Prompt" in event:
            prompt_override = event["Prompt"]
        # The Regenerate button's invoke sets this, because the panel promises
        # that pressing it uses the prompt templates as they are now. An
        # end-of-meeting run does not, and is served from the cache.
        refresh_configuration = bool(event.get("RefreshConfiguration"))
        summary = generate_summary(
            transcript,
            prompt_override,
            refresh_configuration,
            callId,
            agenda=(metadata or {}).get("Agenda"),
        )
        if not prompt_override:
            # only write to S3 when using default summary prompt
            write_to_s3(callId, metadata, transcript, summary)
    except Exception as e:
        print(e)
        summary = "An error occurred."
    print("Returning: ", json.dumps({"summary": summary}))
    return {"summary": summary}


# for testing on terminal
if __name__ == "__main__":
    event = {"CallId": "8cfc6ec4-0dbe-4959-b1f3-34f13359826b"}
    handler(event)
