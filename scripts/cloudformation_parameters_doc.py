#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Generate the parameter reference in `docs/cloudformation-parameters.md`.

The page is generated, between two marker comments, from what `lma-main.yaml`
declares: its parameter groups and labels (the order and headings the
CloudFormation console shows), and each parameter's type, default, allowed values
and description. What the template cannot say -- whether changing a parameter
changes resources or only a value some code reads -- is the classification in
:data:`CLASSIFICATION` below, which has one entry per parameter.

A hand-written page drifts from the template one parameter at a time, and a
reader cannot tell a stale row from a current one. So the test in
`lma-ai-stack/test/test_cloudformation_parameters_doc.py` regenerates the
reference and fails when the committed page differs, when a parameter has no
classification, and when a classification names a parameter the template does
not declare.

Usage::

    python3 scripts/cloudformation_parameters_doc.py           # rewrite the page
    python3 scripts/cloudformation_parameters_doc.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"
DOC = REPO_ROOT / "docs" / "cloudformation-parameters.md"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED PARAMETER REFERENCE: edit lma-main.yaml or "
    "scripts/cloudformation_parameters_doc.py, then run that script -->"
)
END_MARKER = "<!-- END GENERATED PARAMETER REFERENCE -->"


class Kind(NamedTuple):
    """How a parameter takes effect, as the reference describes it."""

    label: str
    meaning: str


RESOURCE = Kind(
    "Resource",
    "Decides which resources the stack creates or how they are built: whether a "
    "nested stack or resource exists, an IAM permission, networking, capacity, a "
    "retention rule or an alarm. Changing it is a stack update that changes resources.",
)
RUNTIME = Kind(
    "Read at run time",
    "Passed to application code as an environment variable or a deployment "
    "setting, and only read by that code. Changing it is a stack update that "
    "changes only the configuration of whatever reads it; for a value built into "
    "the web app, that means the web app is rebuilt. Where the entry says a value "
    "is checked while the stack deploys, a value that fails the check fails the "
    "update.",
)
CREDENTIAL = Kind(
    "Credential",
    "A secret, the ARN of a secret, or the identifier paired with one. Values are "
    "stored in AWS Secrets Manager; see "
    "[Integration Credentials](infrastructure-and-security.md#integration-credentials).",
)
DEPLOY_CHECK = Kind(
    "Deployment check",
    "Read only while the stack is deployed, by a check that runs before anything else is created.",
)
NO_EFFECT = Kind(
    "No effect",
    "Declared by the template and read by nothing. Kept so that existing deploy "
    "commands and pipelines that pass it keep working.",
)
CONFIGURATION_DEFAULT = Kind(
    "Configuration page default",
    "The default of a setting an Admin can change on the web UI's Configuration "
    "page while the stack runs; see the [Configuration Guide](configuration-guide.md). "
    "The stack writes the parameter's value into the configuration as that "
    "setting's default, so a stack update that changes it applies wherever the "
    "setting has not been changed on the page. A value saved on the page takes "
    "precedence until it is reset to the default there.",
)

KINDS = (RESOURCE, CONFIGURATION_DEFAULT, RUNTIME, CREDENTIAL, DEPLOY_CHECK, NO_EFFECT)

#: The configuration layer, whose parameter_defaults.json names the parameters
#: that are the defaults of Configuration page settings, and whose section
#: schemas name those settings as the page shows them.
CONFIGURATION_LAYER = (
    REPO_ROOT / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer" / "lma_config"
)


class Classification(NamedTuple):
    kind: Kind
    #: What the parameter reaches, in a sentence, for the reference entry.
    note: str


def _c(kind: Kind, note: str) -> Classification:
    return Classification(kind, note)


#: One entry per parameter of lma-main.yaml. The note says what the parameter
#: reaches, which is what decides its kind; trace a parameter through the nested
#: stacks before changing its entry.
CLASSIFICATION: Dict[str, Classification] = {
    # Web UI Authentication
    "AdminEmail": _c(
        RESOURCE,
        "Creates the administrator's Cognito user and subscribes the address to the operational alerts topic.",
    ),
    "AllowedSignUpEmailDomain": _c(
        RESOURCE,
        "Decides whether the Cognito sign-up check exists, and is read by it and by user management.",
    ),
    # Virtual Participant Startup Optimization
    "VPLaunchType": _c(
        RESOURCE,
        "Decides whether Virtual Participants run on EC2 hosts, Fargate tasks or Lambda MicroVMs, and which of those resources exist.",
    ),
    "VPInstanceType": _c(
        RESOURCE, "Sets the instance type of the Virtual Participant EC2 launch template."
    ),
    "VPMinInstances": _c(
        RESOURCE, "Sets the minimum and desired size of the Virtual Participant Auto Scaling group."
    ),
    "VPMaxInstances": _c(
        RESOURCE, "Sets the maximum size of the Virtual Participant Auto Scaling group."
    ),
    # Virtual Participant Meeting-End Detection (Teams)
    "VPAttendeePollMs": _c(
        RUNTIME, "Read by the Virtual Participant when it starts, from its task environment."
    ),
    "VPPollsBeforeEnd": _c(
        RUNTIME, "Read by the Virtual Participant when it starts, from its task environment."
    ),
    "VPPollsBeforeEndMissing": _c(
        RUNTIME, "Read by the Virtual Participant when it starts, from its task environment."
    ),
    # Meeting Assist Options
    "MeetingAssistService": _c(
        RESOURCE,
        "Decides whether the meeting assistant's knowledge base is created, used from an existing one, or absent.",
    ),
    "MeetingAssistServiceBedrockModelID": _c(
        RUNTIME,
        "Read by the meeting assistant and the troubleshooting agent. Their Amazon Bedrock permissions cover every model, so the choice changes no permission.",
    ),
    "MeetingAssistKnowledgeBaseModelID": _c(
        RUNTIME, "Read by the meeting assistant's knowledge-base tools."
    ),
    "TavilyApiKey": _c(
        CREDENTIAL, "Stored in a Secrets Manager secret the meeting assistant reads."
    ),
    "TavilyApiKeySecretArn": _c(
        CREDENTIAL, "Grants the meeting assistant read access to the named secret."
    ),
    "AssistantWakePhraseRegEx": _c(
        RUNTIME, "Written to the deployment settings and read by the meeting event processor."
    ),
    # Meeting Assist Voice Assistant
    "ElevenLabsApiKey": _c(
        CREDENTIAL, "Stored in a Secrets Manager secret the Virtual Participant is given."
    ),
    "ElevenLabsApiKeySecretArn": _c(
        CREDENTIAL, "Grants the Virtual Participant read access to the named secret."
    ),
    "ElevenLabsAgentId": _c(
        RUNTIME, "Read by the Virtual Participant when its voice assistant starts."
    ),
    "SimliApiKey": _c(
        CREDENTIAL, "Stored in a Secrets Manager secret the Virtual Participant is given."
    ),
    "SimliApiKeySecretArn": _c(
        CREDENTIAL, "Grants the Virtual Participant read access to the named secret."
    ),
    "SimliFaceId": _c(RUNTIME, "Read by the Virtual Participant when its avatar starts."),
    "VoiceAssistantProvider": _c(RUNTIME, "Read by the Virtual Participant when it starts."),
    "AmazonNovaSonicRegion": _c(
        RESOURCE,
        "Names the region in the Virtual Participant's permission to invoke Amazon Nova Sonic.",
    ),
    "VoiceAssistantActivationMode": _c(
        RUNTIME,
        "Read by the Virtual Participant when it starts, and built into the web app when the stack deploys it, so a change also rebuilds the web app.",
    ),
    "VoiceAssistantWakePhrases": _c(RUNTIME, "Read by the Virtual Participant when it starts."),
    "VoiceAssistantActivationDuration": _c(
        RUNTIME, "Read by the Virtual Participant when it starts."
    ),
    "ZoomMeetingSdkClientId": _c(
        CREDENTIAL,
        "The identifier paired with the Zoom Meeting SDK client secret; with a secret, Zoom meetings join through the SDK.",
    ),
    "ZoomMeetingSdkClientSecret": _c(
        CREDENTIAL, "Stored in a Secrets Manager secret the Virtual Participant is given."
    ),
    "ZoomMeetingSdkClientSecretArn": _c(
        CREDENTIAL, "Grants the Virtual Participant read access to the named secret."
    ),
    "AcsConnectionString": _c(
        CREDENTIAL, "Stored in a Secrets Manager secret the Virtual Participant is given."
    ),
    "AcsConnectionStringSecretArn": _c(
        CREDENTIAL, "Grants the Virtual Participant read access to the named secret."
    ),
    # Meeting Assist Bedrock Guardrails
    "BedrockGuardrailId": _c(
        RESOURCE,
        "Names the guardrail in the meeting assistant's and troubleshooting agent's permission to apply it.",
    ),
    "BedrockGuardrailVersion": _c(
        RUNTIME,
        "Read by the meeting assistant and the troubleshooting agent. The permission to apply the guardrail exists only while both it and BedrockGuardrailId are set.",
    ),
    # Meeting Assist Bedrock Knowledge Base
    "BedrockKnowledgeBaseId": _c(
        RESOURCE,
        "Names the existing knowledge base the meeting assistant is wired to when MeetingAssistService uses one; a template rule requires it then.",
    ),
    "BedrockKnowledgeBaseS3BucketName": _c(
        RESOURCE,
        "Becomes the data source of the knowledge base the stack creates, and the bucket that knowledge base may read.",
    ),
    "BedrockKnowledgeBaseS3DocumentUploadFolderPrefix": _c(
        RESOURCE, "Sets the inclusion prefixes of that data source."
    ),
    # Transcript Knowledge Base Configuration
    "TranscriptKnowledgeBase": _c(
        RESOURCE, "Decides whether the meeting-history knowledge base is created."
    ),
    # LMA Hosted MCP Server
    "EnableMCP": _c(RESOURCE, "Decides whether the hosted MCP server and its resources exist."),
    "MCPServerCallbackURLs": _c(
        RESOURCE, "Sets the OAuth callback URLs of the MCP server's Cognito app client."
    ),
    # Amazon Transcribe Configuration
    "IsPartialTranscriptEnabled": _c(NO_EFFECT, "Partial transcripts are always processed."),
    "IsContentRedactionEnabled": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts. Redaction applies only for the languages Amazon Transcribe redacts.",
    ),
    "TranscribeLanguageCode": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.",
    ),
    "TranscribeLanguageOptions": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.",
    ),
    "TranscribePreferredLanguage": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.",
    ),
    "TranscribeContentRedactionType": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts.",
    ),
    "ShowSpeakerLabel": _c(
        RUNTIME, "Read by the WebSocket transcriber for each streaming session."
    ),
    "TranscribePiiEntityTypes": _c(
        RUNTIME, "Read by the WebSocket transcriber for each streaming session."
    ),
    "CustomVocabularyName": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.",
    ),
    "CustomLanguageModelName": _c(
        RUNTIME,
        "Read by the WebSocket transcriber for each streaming session; checked once while the stack is deployed.",
    ),
    # On-demand speech engine
    "EnableMicrovmAsr": _c(
        RESOURCE, "Decides whether the on-demand speech engine's nested stack exists."
    ),
    # Voice enrollment
    "EnableVoiceEnrollment": _c(
        RESOURCE,
        "Decides whether the enrollment table, the consent log and the enrollment portal exist.",
    ),
    "VoiceEnrollmentRetentionDays": _c(
        RUNTIME,
        "Read by the voice-enrollment functions. It stays a stack parameter by design: the period is stated in the consent terms each enrollment agrees to, so a change is a change of terms.",
    ),
    "VoiceEnrollmentTermsVersion": _c(
        RUNTIME,
        "Read by the voice-enrollment functions. It stays a stack parameter by design: a change asks everybody to consent again.",
    ),
    # WebSocket Transcriber Service Scaling
    "TranscriberDesiredTaskCount": _c(
        RESOURCE, "Sets the task count of the transcriber's ECS service."
    ),
    "TranscriberAutoScalingEnabled": _c(
        RESOURCE, "Decides whether the transcriber's auto scaling target and policy exist."
    ),
    "TranscriberMinTaskCount": _c(
        RESOURCE, "Sets the minimum capacity of the transcriber's auto scaling target."
    ),
    "TranscriberMaxTaskCount": _c(
        RESOURCE, "Sets the maximum capacity of the transcriber's auto scaling target."
    ),
    "TranscriberScalingCpuTargetPercent": _c(
        RESOURCE, "Sets the target of the transcriber's scaling policy."
    ),
    # Transcript Event Processing Configuration
    "TranscriptLambdaHookFunctionArn": _c(
        RESOURCE, "Grants the meeting event processor permission to invoke the named function."
    ),
    "TranscriptLambdaHookFunctionNonPartialOnly": _c(
        RUNTIME, "Read by the meeting event processor."
    ),
    "EndOfCallTranscriptSummary": _c(
        RESOURCE,
        "Decides whether the Bedrock summarizer exists or a Lambda hook is invoked instead.",
    ),
    "BedrockModelId": _c(
        RUNTIME,
        "Read by the summarizer and the functions that parse invitations, name speakers, derive agendas, answer transcript knowledge-base queries and analyse MCP usage. Their Amazon Bedrock permissions cover every model, so the choice changes no permission.",
    ),
    "KeyframeTextModelId": _c(
        RUNTIME, "Read by the function that reads the text of each key frame."
    ),
    "EndOfCallLambdaHookFunctionArn": _c(
        RESOURCE, "Grants the summary functions permission to invoke the named function."
    ),
    "StartOfCallLambdaHookFunctionArn": _c(
        RESOURCE, "Grants the meeting event processor permission to invoke the named function."
    ),
    "PostCallSummaryLambdaHookFunctionArn": _c(
        RESOURCE, "Grants the meeting event processor permission to invoke the named function."
    ),
    # Amazon CloudFront Configuration
    "CloudFrontPriceClass": _c(
        RESOURCE,
        "Sets the price class of the web app's and the WebSocket transcriber's CloudFront distributions.",
    ),
    "CloudFrontAllowedGeos": _c(
        RESOURCE, "Sets the geographic restriction of the web app's CloudFront distribution."
    ),
    # Retention Settings
    "EnableAudioRecording": _c(
        RUNTIME,
        "Read by the WebSocket transcriber as the default for each meeting, and by the Virtual Participant when it starts.",
    ),
    "EnableVideoRecording": _c(
        RESOURCE,
        "Sizes the transcriber's task for video, and limits the transcriber to one task through a template rule.",
    ),
    "MeetingRecordExpirationInDays": _c(
        RUNTIME,
        "Read by every function and task that writes meeting records, to set when each record expires.",
    ),
    "TranscriptionExpirationInDays": _c(
        RESOURCE,
        "Sets a lifecycle rule on the recordings bucket, and is read by the meeting event processor to set when transcript segments expire.",
    ),
    "AudioRecordingExpirationInDays": _c(
        RESOURCE,
        "Sets the lifecycle rules for audio and video recordings on the recordings bucket.",
    ),
    "CloudWatchLogsExpirationInDays": _c(
        RESOURCE, "Sets the retention of every log group the stack declares."
    ),
    "VPSchedulerStateMachineLogsExpirationInDays": _c(
        RESOURCE,
        "Sets the retention of the scheduled Virtual Participant state machine's log group.",
    ),
    "MeetingInactivityTimeoutInMinutes": _c(
        RESOURCE,
        "Decides whether the inactive-meeting reaper and its schedule exist, and is read by the reaper.",
    ),
    "MeetingInactivityLookbackInDays": _c(RUNTIME, "Read by the inactive-meeting reaper."),
    "EnableDataRetentionOnDelete": _c(
        RESOURCE, "Sets the deletion policy of the stack's tables, buckets, user pool and keys."
    ),
    # AppSync Configuration
    "EnableAppSyncApiCache": _c(RESOURCE, "Decides whether the AppSync API cache exists."),
    "AppSyncApiCacheInstanceType": _c(RESOURCE, "Sets the instance type of the AppSync API cache."),
    # VPC Configuration
    "UseExistingVPC": _c(RESOURCE, "Decides whether the stack creates its own VPC."),
    "VPC": _c(RESOURCE, "The VPC the load balancers, security groups and tasks are placed in."),
    "PublicSubnet1": _c(RESOURCE, "A subnet the load balancers are placed in."),
    "PublicSubnet2": _c(RESOURCE, "A subnet the load balancers are placed in."),
    "PrivateSubnet1": _c(
        RESOURCE, "A subnet the transcriber and Virtual Participant tasks are placed in."
    ),
    "PrivateSubnet2": _c(
        RESOURCE, "A subnet the transcriber and Virtual Participant tasks are placed in."
    ),
    # Operational Monitoring
    "EnableOperationalAlarms": _c(RESOURCE, "Decides whether the operational alarms exist."),
    "TranscribeConcurrentStreamAlarmThreshold": _c(
        RESOURCE,
        "Sets the threshold of the concurrent-stream alarm and the dashboard line drawn at it.",
    ),
    "LogLevel": _c(
        RUNTIME,
        "Read by each function and task's logging when the process starts, so a change applies as processes are replaced.",
    ),
    # Security Configuration
    "PermissionsBoundaryArn": _c(
        RESOURCE, "Attached as the permissions boundary of every IAM role the stack creates."
    ),
    # Workshop options
    "ModelValidation": _c(
        DEPLOY_CHECK,
        "Decides whether the deployment checks that the chosen Amazon Bedrock models can be invoked.",
    ),
}


# ── reading the template ─────────────────────────────────────────────────────


class _Loader(yaml.SafeLoader):
    """Reads CloudFormation's short-form tags as plain values."""


def _tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


_Loader.add_multi_constructor("!", _tag)


def load_template(path: Path = MAIN_TEMPLATE) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_Loader)  # noqa: S506 -- SafeLoader subclass


def parameter_groups(template: Mapping[str, Any]) -> List[Tuple[str, List[str]]]:
    """The console's groups, in order, holding every declared parameter once.

    A parameter no group lists is reported under "Other parameters", so a new
    parameter appears in the reference before anyone has placed it.
    """
    interface = template.get("Metadata", {}).get("AWS::CloudFormation::Interface", {})
    declared = template["Parameters"]
    groups: List[Tuple[str, List[str]]] = []
    seen: set = set()
    for group in interface.get("ParameterGroups", []):
        names = [
            name for name in group.get("Parameters", []) if name in declared and name not in seen
        ]
        seen.update(names)
        if names:
            groups.append((group["Label"]["default"], names))
    rest = [name for name in declared if name not in seen]
    if rest:
        groups.append(("Other parameters", rest))
    return groups


# ── the parameters that are Configuration page defaults ─────────────────────


def configuration_defaults() -> Dict[str, Tuple[str, str]]:
    """Parameter name to (section label, setting title), as the page shows them.

    A setting the page does not show -- its schema marks it ``hidden``, because
    it has one possible value -- is named with "(not shown on the page)", so
    the reference does not send a reader looking for it.

    Read from the configuration layer rather than restated here, so the
    reference cannot call a parameter a Configuration page default that the
    seeder does not write, or miss one that it does.
    """
    mapping = json.loads((CONFIGURATION_LAYER / "parameter_defaults.json").read_text("utf-8"))
    result = {}
    for parameter, entry in mapping.items():
        schema = json.loads(
            (CONFIGURATION_LAYER / "schemas" / f"{entry['section']}.json").read_text("utf-8")
        )
        setting = schema["properties"][entry["setting"]]
        title = setting.get("title", entry["setting"])
        if setting.get("hidden"):
            title += " (not shown on the page)"
        result[parameter] = (schema["sectionLabel"], title)
    return result


def classification_of(name: str, defaults: Mapping[str, Tuple[str, str]]) -> Classification:
    """The classification the reference shows: CLASSIFICATION's, unless the
    parameter is the default of a Configuration page setting."""
    classification = CLASSIFICATION[name]
    if name not in defaults:
        return classification
    section, setting = defaults[name]
    return Classification(
        CONFIGURATION_DEFAULT,
        f"Configuration page: **{section} › {setting}**. {classification.note}",
    )


# ── rendering ────────────────────────────────────────────────────────────────


def _text(value: Any) -> str:
    """Template prose as one Markdown line.

    Angle brackets are escaped because the documentation site reads a bare
    `<region>` as an HTML element and drops it from the page.
    """
    collapsed = re.sub(r"\s+", " ", str(value)).strip()
    return collapsed.replace("<", "&lt;").replace(">", "&gt;")


def _code(value: Any) -> str:
    text = str(value)
    fence = "``" if "`" in text else "`"
    return f"{fence}{text}{fence}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def describe_default(spec: Mapping[str, Any]) -> str:
    if "Default" not in spec:
        return "(required)"
    default = spec["Default"]
    if default == "" or default is None:
        return "(empty)"
    return _code(default)


def describe_values(spec: Mapping[str, Any]) -> str:
    if "AllowedValues" in spec:
        return ", ".join(_code(value) for value in spec["AllowedValues"])
    if "MinValue" in spec or "MaxValue" in spec:
        low, high = spec.get("MinValue", ""), spec.get("MaxValue", "")
        if low != "" and high != "":
            return f"{low} to {high}"
        return f"at least {low}" if low != "" else f"at most {high}"
    if "ConstraintDescription" in spec:
        return _text(spec["ConstraintDescription"])
    if "AllowedPattern" in spec:
        return f"Matches {_code(spec['AllowedPattern'])}"
    return "Any " + ("number" if spec.get("Type") == "Number" else "text")


def anchor(name: str) -> str:
    """The anchor a `### `Name`` heading gets: the name, lower-cased."""
    return name.lower()


def render_reference(template: Mapping[str, Any]) -> str:
    declared = template["Parameters"]
    labels = (
        template.get("Metadata", {})
        .get("AWS::CloudFormation::Interface", {})
        .get("ParameterLabels", {})
    )
    groups = parameter_groups(template)
    defaults = configuration_defaults()
    lines: List[str] = [BEGIN_MARKER, ""]

    counts = {kind.label: 0 for kind in KINDS}
    for name in declared:
        counts[classification_of(name, defaults).kind.label] += 1
    lines += [
        f"The template declares {len(declared)} parameters.",
        "",
        "| Kind | Parameters | What changing one does |",
        "|---|---|---|",
    ]
    lines += [f"| {kind.label} | {counts[kind.label]} | {kind.meaning} |" for kind in KINDS]
    lines.append("")

    for title, names in groups:
        lines += [f"## {title}", "", "| Parameter | Default | Kind |", "|---|---|---|"]
        for name in names:
            spec = declared[name]
            lines.append(
                f"| [`{name}`](#{anchor(name)}) | {_cell(describe_default(spec))} | "
                f"{classification_of(name, defaults).kind.label} |"
            )
        lines.append("")
        for name in names:
            spec = declared[name]
            classification = classification_of(name, defaults)
            label = labels.get(name, {}).get("default")
            lines += [f"### `{name}`", ""]
            if label:
                lines += [f"Console label: *{_text(label)}*", ""]
            lines += [
                f"- **Default:** {describe_default(spec)}",
                f"- **Allowed values:** {describe_values(spec)}",
                f"- **Kind:** {classification.kind.label}. {classification.note}",
                "",
            ]
            description = _text(spec.get("Description", ""))
            if description:
                lines += [description, ""]
    lines.append(END_MARKER)
    return "\n".join(lines)


def render_page(current: str, template: Mapping[str, Any]) -> str:
    """The page with its generated block replaced, everything else untouched."""
    start = current.index(BEGIN_MARKER)
    end = current.index(END_MARKER) + len(END_MARKER)
    return current[:start] + render_reference(template) + current[end:]


def classification_problems(template: Mapping[str, Any]) -> List[str]:
    declared = set(template["Parameters"])
    problems = [
        f"{name} has no entry in CLASSIFICATION" for name in sorted(declared - set(CLASSIFICATION))
    ]
    problems += [
        f"CLASSIFICATION names {name}, which lma-main.yaml does not declare"
        for name in sorted(set(CLASSIFICATION) - declared)
    ]
    # A setting an Admin changes at run time is one only code reads, so its
    # parameter must have been classified as read at run time. Anything else
    # means the parameter also reaches a resource, which the page cannot change.
    for name in sorted(configuration_defaults()):
        if name not in declared:
            problems.append(
                f"parameter_defaults.json names {name}, which lma-main.yaml does not declare"
            )
        elif name in CLASSIFICATION and CLASSIFICATION[name].kind is not RUNTIME:
            problems.append(
                f"{name} is the default of a Configuration page setting, but CLASSIFICATION "
                f"says it is {CLASSIFICATION[name].kind.label!r}, not {RUNTIME.label!r}"
            )
    return problems


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the page is out of date")
    args = parser.parse_args(argv)

    template = load_template()
    problems = classification_problems(template)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    current = DOC.read_text(encoding="utf-8")
    expected = render_page(current, template)
    if args.check:
        if expected != current:
            print(
                f"{DOC.relative_to(REPO_ROOT)} is out of date; run {Path(__file__).name}",
                file=sys.stderr,
            )
            return 1
        return 0
    DOC.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
