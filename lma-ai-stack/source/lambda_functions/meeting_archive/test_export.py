# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting export (no AWS calls).

Asserted here: an export of a meeting with everything, and of one whose
transcript was deleted, each validate against the published schema
(docs/schemas/meeting-export-v1.schema.json); participants are named only from
a name somebody gave or confirmed, with where it came from; no image, link to
an image or evidence is in it; ``include`` chooses the parts; the export is
given to exactly the callers ``getCall`` answers; a large one is answered with a
link signed for ten minutes to an object under the archive prefix, written only
after the caller's access was checked; and the archive holds it.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import export
import jsonschema
import pytest
from test_index import (
    CALL,
    FRIEND,
    OWNER,
    POSIX,
    Context,
    get,
    index,
    meeting_item,
    partitions,
    prepare,
    s,
)

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[4] / "docs" / "schemas" / "meeting-export-v1.schema.json"
    ).read_text(encoding="utf-8")
)
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())


def valid(document) -> None:
    errors = sorted(VALIDATOR.iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def n(value):
    return {"N": str(value)}


IDENTITIES = {
    "identities": {
        # Named by a person on the Speakers panel.
        "REMOTE#Other Participant (spk_0)": {
            "name": "Priya Shah",
            "status": "confirmed",
            "source": "manual",
        },
        # Named from a self-introduction in the transcript.
        "REMOTE#Other Participant (spk_1)": {
            "name": "Sam Lee",
            "status": "confirmed",
            "source": "self-introduction",
        },
        # Suggested and not confirmed: not a name yet.
        "REMOTE#Other Participant (spk_2)": {
            "name": "Jordan",
            "status": "proposed",
            "source": "addressed-by-name",
        },
    }
}


def segment(segment_id, channel, speaker, text, start, end=None, partial=False):
    return {
        "PK": s(f"trs#{CALL}"),
        "SK": s(f"ts#{start}#{segment_id}"),
        "SegmentId": s(segment_id),
        "Channel": s(channel),
        "Speaker": s(speaker),
        "Transcript": s(text),
        "StartTime": n(start),
        "EndTime": n(end if end is not None else start + 2),
        "IsPartial": {"BOOL": partial},
    }


def full_partitions():
    parts = partitions()
    parts[f"trs#{CALL}"] = [
        segment("s1", "REMOTE", "Other Participant (spk_0)", "We need SSO support.", 1.0),
        segment("s2", "REMOTE", "Other Participant (spk_1)", "I'm Sam Lee, from finance.", 4.0),
        segment("s3", "REMOTE", "Other Participant (spk_2)", "Thanks, Jordan.", 7.0),
        segment("s4", "LOCAL", "owner@example.com", "Let's agree the plan.", 9.0),
        segment("s5", "CALLER", "Bob Strahan", "Older channel name.", 11.0),
        segment("c1", "MEETING_CHAT", "Priya Shah", "Spec: https://docs.example.com/spec", 12.0),
        segment("c2", "MEETING_CHAT", "Priya Shah", "again https://docs.example.com/spec", 13.0),
        segment("a1", "AGENT_ASSISTANT", "MEETING ASSISTANT", "An answer.", 14.0),
        segment("p1", "REMOTE", "Other Participant (spk_0)", "partial", 15.0, partial=True),
    ]
    parts[f"kf#{CALL}"] = [
        {
            "SK": s("f#0000030000-ab12cd"),
            "ImageUrl": s("https://rec-bucket.s3.us-east-1.amazonaws.com/x.jpg"),
            "Timestamp": n(30),
            "Title": s("Roadmap"),
            "SlideText": s("Roadmap Q1 Q2"),
        },
        {"SK": s("f#cam"), "Timestamp": n(40), "ContentKind": s("CAMERA"), "SlideText": s("faces")},
        {"SK": s("f#dup"), "Timestamp": n(45), "DuplicateOf": s("0000030000-ab12cd")},
        {"SK": s("f#untitled"), "Timestamp": n(70)},
    ]
    parts[f"nt#{CALL}"] = [
        {
            "PK": s(f"nt#{CALL}"),
            "SK": s("n#2"),
            "Category": s("guidance"),
            "Type": s("ask_question"),
            "Title": s("Ask about budget"),
            "Body": s("Who owns it?"),
            "Refs": {"L": [s("s1")]},
            "CreatedAt": s("2026-11-03T14:10:00.000Z"),
        },
        {"PK": s(f"nt#{CALL}"), "SK": s("n#3"), "Category": s("consent"), "Title": s("other")},
        {"PK": s(f"nt#{CALL}"), "SK": s("d#guidance#k"), "Category": s("guidance")},
    ]
    panel = {
        "M": {
            "PanelId": s("decisions"),
            "Title": s("Decisions"),
            "Kind": s("markdown"),
            "Content": s("- Ship in March"),
            "Source": s("assistant"),
            "UpdatedBy": s("LMA assistant"),
            "UpdatedAt": s("2026-11-03T14:20:00.000Z"),
        }
    }
    diagram = {
        "M": {
            "PanelId": s("diagram"),
            "Title": s("Flow"),
            "Kind": s("mermaid"),
            "Content": s("flowchart TD\n  a-->b"),
            "Source": s("user"),
            "UpdatedBy": s("owner@example.com"),
            "UpdatedAt": s("2026-11-03T14:30:00.000Z"),
        }
    }
    board = {
        "PK": s(f"wb#{CALL}"),
        "WhiteboardId": s("wb1"),
        "Title": s("Plan"),
        "Template": s("decisions"),
        "Instructions": s(""),
        "AssistantUpdates": {"BOOL": True},
        "Version": n(3),
        "CreatedBy": s("owner@example.com"),
        "CreatedAt": s("2026-11-03T14:00:00.000Z"),
        "UpdatedBy": s("owner@example.com"),
        "UpdatedAt": s("2026-11-03T14:30:00.000Z"),
    }
    parts[f"wb#{CALL}"] = [
        {**board, "SK": s("b#wb1"), "Panels": {"L": [panel, diagram]}},
        {**board, "SK": s("v#wb1#000001"), "Version": n(1), "Panels": {"L": [panel]}},
        {"PK": s(f"wb#{CALL}"), "SK": s("index"), "Boards": n(1)},
    ]
    return parts


def full_meeting(**extra):
    return meeting_item(
        SpeakerIdentities=s(json.dumps(IDENTITIES)),
        MeetingPlatform=s("Zoom"),
        CaptureSource=s("VIRTUAL_PARTICIPANT"),
        UpdatedAt=s("2026-11-03T15:05:00.000Z"),
        Labels={"L": [{"M": {"Key": s("project"), "Value": s("acme-claims")}}]},
        Audience=s("external"),
        SummaryProfile=s("discovery-session"),
        CallSummaryState=s(
            json.dumps({"source": "generated", "status": "idle", "profile": "discovery-session"})
        ),
        Agenda=s("Scope\nRisks"),
        AgendaSource=s("mcp"),
        MeetingContext=s("# Goals"),
        Checklist={
            "L": [
                {
                    "M": {
                        "ItemId": s("q1"),
                        "Text": s("Who owns the budget?"),
                        "Kind": s("question"),
                        "Status": s("covered"),
                        "Evidence": {
                            "L": [
                                {
                                    "M": {
                                        "SegmentId": s("s1"),
                                        "StartTime": n(1),
                                        "Quote": s("We need SSO"),
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
        ActionItems={
            "L": [
                {
                    "M": {
                        "Text": s("Send the plan"),
                        "Owner": s("Priya Shah"),
                        "DueDate": s("2026-12-01"),
                        "DueText": s("by the first"),
                        "StartTime": n(9),
                        "SegmentId": s("s4"),
                    }
                },
                {
                    "M": {
                        "Text": s("Book a follow-up"),
                        "Owner": {"NULL": True},
                        "DueDate": {"NULL": True},
                    }
                },
            ]
        },
        **extra,
    )


def export_of(fakes, meeting=None, include=None, parts=None):
    fakes(
        meeting=meeting or full_meeting(),
        parts=full_partitions() if parts is None else parts,
    )
    call_id = CALL
    return index.meeting_export(call_id, index.read_meeting(call_id), include)


# ── the format ──────────────────────────────────────────────────────────────


def test_a_meeting_with_everything_matches_the_schema(fakes) -> None:
    document = export_of(fakes)
    valid(document)
    assert document["schemaVersion"] == 1
    meeting = document["meeting"]
    assert meeting["name"] == "Weekly sync"
    assert meeting["platform"] == "Zoom"
    assert meeting["labels"] == {"project": "acme-claims"}
    assert meeting["summaryProfile"] == "discovery-session"
    assert meeting["transcriptRetained"] is True
    assert meeting["sharedWith"] == ["friend@example.com"]
    assert meeting["endTime"] == "2026-11-03T15:05:00.000Z"
    assert document["agenda"] == {"text": "Scope\nRisks", "source": "mcp"}
    assert document["summary"]["profile"] == "discovery-session"
    assert document["checklist"][0]["evidence"][0]["quote"] == "We need SSO"


def test_the_format_is_named_first_so_a_file_is_recognised_by_its_content(fakes) -> None:
    for document in (export_of(fakes), export_of(fakes, include=["summary"])):
        assert list(document)[:2] == ["format", "schemaVersion"]
        assert document["format"] == "lma-meeting-export"
    assert export.FORMAT == "lma-meeting-export"


def test_an_export_without_the_format_still_matches_the_schema(fakes) -> None:
    """format is optional in the schema, and must be lma-meeting-export when given."""
    document = export_of(fakes)
    del document["format"]
    valid(document)
    document["format"] = "something-else"
    assert list(VALIDATOR.iter_errors(document))


def test_checklist_evidence_and_action_items_carry_their_segment_s_times(fakes) -> None:
    document = export_of(fakes)
    assert document["checklist"][0]["evidence"] == [
        {"segmentId": "s1", "startTime": 1.0, "endTime": 3.0, "quote": "We need SSO"}
    ]
    first, second = document["actionItems"]
    assert (first["segmentId"], first["startTime"], first["endTime"]) == ("s4", 9.0, 11.0)
    # No segment named: no time is made up.
    assert (second["segmentId"], second["startTime"], second["endTime"]) == (None, None, None)


def test_evidence_with_a_segment_id_alone_takes_both_times_from_the_segment(fakes) -> None:
    item = {
        "M": {
            "ItemId": s("q2"),
            "Text": s("Timeline?"),
            "Kind": s("question"),
            "Evidence": {
                "L": [
                    {"M": {"SegmentId": s("s2")}},
                    {"M": {"SegmentId": s("not-a-segment"), "StartTime": n(50)}},
                    {"M": {"Quote": s("only a quote")}},
                ]
            },
        }
    }
    meeting = full_meeting()
    meeting["Checklist"] = {"L": [item]}
    document = export_of(fakes, meeting=meeting)
    valid(document)
    assert [
        (e["segmentId"], e["startTime"], e["endTime"]) for e in document["checklist"][0]["evidence"]
    ] == [
        ("s2", 4.0, 6.0),
        ("not-a-segment", 50.0, None),
        (None, None, None),
    ]


def test_the_checklist_alone_still_reads_the_segments_for_its_times(fakes) -> None:
    document = export_of(fakes, include=["checklist", "actionItems"])
    valid(document)
    assert "transcript" not in document and "participants" not in document
    assert document["checklist"][0]["evidence"][0]["endTime"] == 3.0
    assert document["actionItems"][0]["endTime"] == 11.0


def test_a_participant_s_consent_is_unknown_without_a_recorded_link(fakes) -> None:
    """A consent event under a participant's exact name is not matched to them."""
    consent = {
        "M": {
            "Mode": s("opt_in"),
            "Rule": s("each_participant"),
            "Status": s("granted"),
            "Events": {
                "L": [
                    {
                        "M": {
                            "ParticipantName": s("Priya Shah"),
                            "Decision": s("consent"),
                            "Source": s("chat"),
                        }
                    },
                    {
                        "M": {
                            "ParticipantName": s("Sam Lee"),
                            "Decision": s("objection"),
                            "Source": s("voice"),
                        }
                    },
                ]
            },
        }
    }
    document = export_of(fakes, meeting=full_meeting(Consent=consent))
    valid(document)
    assert {p["consent"] for p in document["participants"]} == {"unknown"}


@pytest.mark.parametrize(
    "decisions, expected",
    [
        ([], "unknown"),
        ([("consent", "chat")], "granted"),
        ([("objection", "voice")], "objected"),
        ([("objection", "chat"), ("consent", "voice")], "granted"),
        ([("consent", "chat"), ("objection", "ui")], "objected"),
        ([("consent", "attestation")], "unknown"),
    ],
)
def test_a_participant_s_consent_follows_their_latest_linked_reply(decisions, expected) -> None:
    linked = [
        {"speakerId": "remote-spk_0", "decision": decision, "source": source}
        for decision, source in decisions
    ]
    other = [{"speakerId": "remote-spk_1", "decision": "objection", "source": "chat"}]
    unlinked = [{"speakerId": None, "decision": "objection", "source": "chat"}]
    assert export.participant_consent("remote-spk_0", linked + other + unlinked) == expected
    assert export.participant_consent(None, unlinked) == "unknown"


CONSENT_RECORD = {
    "M": {
        "Mode": s("opt_in"),
        "Rule": s("each_participant"),
        "Status": s("granted"),
        "CaptureAllowed": {"BOOL": True},
        "NoticePostedAt": s("2026-11-03T14:05:30Z"),
        "Notices": {
            "L": [
                {
                    "M": {
                        "Channel": s("chat"),
                        "Text": s("LMA is recording"),
                        "PostedAt": s("2026-11-03T14:05:30Z"),
                        "PostedBy": s("vp"),
                    }
                }
            ]
        },
        "Events": {
            "L": [
                {
                    "M": {
                        "EventId": s("e1"),
                        "ParticipantName": s("Priya Shah"),
                        "Decision": s("objection"),
                        "Source": s("chat"),
                        "Text": s("no thanks"),
                        "OccurredAt": s("2026-11-03T14:06:00Z"),
                        "RecordedAt": s("2026-11-03T14:06:01Z"),
                    }
                },
                {
                    "M": {
                        "ParticipantName": s("Priya Shah"),
                        "Decision": s("consent"),
                        "Source": s("voice"),
                        "RecordedAt": s("2026-11-03T14:07:00Z"),
                    }
                },
                {
                    "M": {
                        "ParticipantName": s("owner@example.com"),
                        "Decision": s("consent"),
                        "Source": s("attestation"),
                        "OccurredAt": s("2026-11-03T14:08:00Z"),
                    }
                },
            ]
        },
        "Attestation": {
            "M": {
                "AttestedBy": s("owner@example.com"),
                "Method": s("verbal"),
                "Statement": s("Everyone agreed"),
                "AttestedAt": s("2026-11-03T14:08:00Z"),
            }
        },
        "Evidence": {
            "L": [
                {
                    "M": {
                        "EvidenceId": s("ev1"),
                        "S3Key": s(f"lma-consent-evidence/{POSIX}/evidence-1.jpg"),
                    }
                }
            ]
        },
        "StartedAt": s("2026-11-03T14:05:10Z"),
        "UpdatedAt": s("2026-11-03T14:08:00Z"),
    }
}


def test_the_export_s_consent_is_the_record_as_get_meeting_consent_gives_it(fakes) -> None:
    """One shape for both: the export's part equals the MCP tool's answer for the same record."""
    from lma_consent.summary import meeting_consent_output

    meeting = full_meeting(Consent=CONSENT_RECORD)
    document = export_of(fakes, meeting=meeting)
    valid(document)
    stored = index.read_meeting(CALL)["Consent"]
    # What getMeetingConsent answers with, minus its signed links, as the MCP tool reads it.
    answer = meeting_consent_output(
        CALL, {"CallId": CALL, "CaptureAllowed": True, "Consent": stored}
    )
    assert document["consent"] == answer
    assert document["consent"] == {
        "meetingId": CALL,
        "mode": "opt_in",
        "rule": "each_participant",
        "status": "granted",
        "captureAllowed": True,
        "noticePostedAt": "2026-11-03T14:05:30Z",
        "notices": [{"channel": "chat", "postedAt": "2026-11-03T14:05:30Z"}],
        "events": [
            {
                "participantName": "Priya Shah",
                "decision": "objection",
                "source": "chat",
                "at": "2026-11-03T14:06:00Z",
                "speakerId": None,
            },
            {
                "participantName": "Priya Shah",
                "decision": "consent",
                "source": "voice",
                "at": "2026-11-03T14:07:00Z",
                "speakerId": None,
            },
            {
                "participantName": "owner@example.com",
                "decision": "consent",
                "source": "attestation",
                "at": "2026-11-03T14:08:00Z",
                "speakerId": None,
            },
        ],
        "attestation": {
            "attestedBy": "owner@example.com",
            "method": "verbal",
            "at": "2026-11-03T14:08:00Z",
        },
        "hasEvidence": True,
        "startedAt": "2026-11-03T14:05:10Z",
        "updatedAt": "2026-11-03T14:08:00Z",
    }
    # No notice text, reply words, evidence key or other stored detail is copied.
    text = json.dumps(document["consent"])
    assert "no thanks" not in text and "LMA is recording" not in text and ".jpg" not in text


def test_participants_are_named_only_from_a_name_given_or_confirmed(fakes) -> None:
    people = {p["speakerId"]: p for p in export_of(fakes)["participants"]}
    assert people["remote-spk_0"]["name"] == "Priya Shah"
    assert people["remote-spk_0"]["nameSource"] == "manual"
    assert people["remote-spk_1"]["name"] == "Sam Lee"
    assert people["remote-spk_1"]["nameSource"] == "transcript"
    # A suggestion nobody confirmed is not a name.
    assert people["remote-spk_2"]["name"] is None
    assert people["remote-spk_2"]["nameSource"] == "none"
    # A name the capture client or platform gave the channel.
    assert people["local-1"]["name"] == "owner@example.com"
    assert people["local-1"]["nameSource"] == "platform"
    # Stored under the channel's earlier name, read under its current one.
    assert people["remote-1"]["name"] == "Bob Strahan"
    assert people["chat-1"]["name"] == "Priya Shah"
    assert not any(p["nameSource"] == "enrolled" for p in people.values())
    # Every speaker id in the transcript is a participant.
    ids = {e["speakerId"] for e in export_of(fakes)["transcript"] if e["speakerId"]}
    assert ids <= set(people)


def test_the_transcript_holds_speech_chat_and_screen_text_but_not_the_assistant(fakes) -> None:
    transcript = export_of(fakes)["transcript"]
    kinds = [(e["segmentId"], e["kind"]) for e in transcript]
    assert ("a1", "speech") not in kinds and "p1" not in [k for k, _ in kinds]
    assert ("c1", "chat") in kinds
    assert ("keyframe-0000030000-ab12cd", "screen") in kinds
    # The camera's frames and repeats are not screen text.
    assert not any(e["segmentId"] in ("keyframe-cam", "keyframe-dup") for e in transcript)
    assert [e["startTime"] for e in transcript] == sorted(e["startTime"] for e in transcript)
    assert next(e for e in transcript if e["segmentId"] == "s5")["channel"] == "REMOTE"


def test_links_are_listed_once_by_the_participant_who_shared_them(fakes) -> None:
    document = export_of(fakes)
    assert document["links"] == [
        {"url": "https://docs.example.com/spec", "startTime": 12.0, "sharedBy": "chat-1"}
    ]


def test_no_image_or_evidence_is_in_the_export(fakes) -> None:
    document = export_of(fakes)
    text = json.dumps(document)
    assert "keyframe:" not in text and "ImageUrl" not in text and ".jpg" not in text
    assert "(Slide 1)" in json.dumps(document["summary"])
    assert document["consent"]["hasEvidence"] is True
    assert "evidence" not in document["consent"]
    assert [f["altText"] for f in document["keyframes"]] == ["Roadmap", "Shared screen at 01:10"]


def test_guidance_is_the_guidance_category_alone(fakes) -> None:
    assert export_of(fakes)["guidance"] == [
        {
            "type": "ask_question",
            "title": "Ask about budget",
            "body": "Who owns it?",
            "refs": ["s1"],
            "createdAt": "2026-11-03T14:10:00.000Z",
        }
    ]


def test_whiteboards_are_given_as_they_are_now_without_earlier_versions(fakes) -> None:
    boards = export_of(fakes)["whiteboards"]
    assert [b["whiteboardId"] for b in boards] == ["wb1"]
    assert boards[0]["version"] == 3 and boards[0]["assistantUpdates"] is True
    assert [(p["title"], p["kind"], p["source"]) for p in boards[0]["panels"]] == [
        ("Decisions", "markdown", "assistant"),
        ("Flow", "mermaid", "user"),
    ]


def test_action_items_say_plainly_when_there_is_no_owner(fakes) -> None:
    items = export_of(fakes)["actionItems"]
    assert items[0]["owner"] == "Priya Shah" and items[0]["segmentId"] == "s4"
    assert items[1]["owner"] is None and items[1]["dueDate"] is None


def test_a_meeting_whose_transcript_was_deleted_matches_the_schema_without_it(fakes) -> None:
    document = export_of(fakes, meeting=full_meeting(TranscriptDeletedAt=s("2026-11-04T00:00:00Z")))
    valid(document)
    assert document["meeting"]["transcriptRetained"] is False
    assert document["meeting"]["transcriptDeletedAt"] == "2026-11-04T00:00:00Z"
    assert "transcript" not in document and "links" not in document
    assert "participants" not in document
    # The segment is deleted with the transcript, so its end time is not known.
    assert document["checklist"][0]["evidence"] == [
        {"segmentId": "s1", "startTime": 1.0, "endTime": None, "quote": None}
    ]
    assert document["actionItems"][0]["startTime"] == 9.0
    assert document["actionItems"][0]["endTime"] is None
    # The summary is kept, as the meeting keeps it.
    assert document["summary"]["sections"]
    # The whiteboards are drawn from the transcript, and deleted with it.
    assert document["whiteboards"] == []


def test_a_meeting_with_nothing_but_its_record_matches_the_schema(fakes) -> None:
    bare = meeting_item(CallSummaryText={"NULL": True}, Consent={"NULL": True})
    document = export_of(fakes, meeting=bare, parts={})
    valid(document)
    assert document["meeting"]["summaryProfile"] == "default"
    assert document["consent"] == {
        "meetingId": CALL,
        "mode": "off",
        "rule": None,
        "status": "not_required",
        "captureAllowed": None,
        "noticePostedAt": None,
        "notices": [],
        "events": [],
        "attestation": None,
        "hasEvidence": False,
        "startedAt": None,
        "updatedAt": None,
    }
    assert "summary" not in document
    assert document["transcript"] == [] and document["actionItems"] == []


def test_include_chooses_the_parts(fakes) -> None:
    document = export_of(fakes, include=["summary", "actionItems"])
    valid(document)
    assert set(document) == {
        "format",
        "schemaVersion",
        "exportedAt",
        "lmaVersion",
        "meeting",
        "agenda",
        "context",
        "summary",
        "actionItems",
    }
    chat_only = export_of(fakes, include=["chat"])
    assert {e["kind"] for e in chat_only["transcript"]} == {"chat"}
    with pytest.raises(ValueError, match="unknown part"):
        export.parse_include(["recordings"])


def test_section_labels_read_as_headings() -> None:
    assert export.section_label("ACTION ITEMS") == "Action items"
    assert export.section_label("Summary") == "Summary"


# ── the MCP server's request ────────────────────────────────────────────────


def mcp(identity, include=None):
    event = {"Trigger": "export", "CallId": CALL, "identity": identity}
    if include is not None:
        event["Include"] = include
    return index.handler(event, Context())


def mcp_identity(who: str, admin: bool = False):
    """The identity the MCP server passes (access.resolver_identity)."""
    return {"username": who, "groups": ["Admin"] if admin else []}


@pytest.mark.parametrize(
    "identity",
    [
        mcp_identity("owner@example.com"),
        mcp_identity("friend@example.com"),
        mcp_identity("someone@example.com", admin=True),
    ],
    ids=["owner", "shared", "admin"],
)
def test_the_export_is_given_to_whoever_may_see_the_meeting(fakes, identity) -> None:
    fakes(meeting=full_meeting(), parts=full_partitions())
    answer = mcp(identity)
    valid(answer["export"])
    # The image-less consent record, for everyone who can see the meeting.
    assert answer["export"]["consent"]["mode"] == "opt_in"


def test_the_export_is_refused_to_anyone_else_and_for_a_missing_meeting(fakes) -> None:
    _dynamo, s3, _ = fakes(meeting=full_meeting(), parts=full_partitions())
    with pytest.raises(RuntimeError, match="do not have access"):
        mcp(mcp_identity("x@example.com"))
    fakes(meeting={}, parts={})
    with pytest.raises(RuntimeError, match="do not have access"):
        mcp(mcp_identity("owner@example.com"))
    assert s3.stored == {}


def test_an_unknown_part_is_refused(fakes) -> None:
    fakes(meeting=full_meeting(), parts=full_partitions())
    with pytest.raises(RuntimeError, match="unknown part"):
        mcp(mcp_identity("owner@example.com"), include=["everything"])


def test_a_large_export_is_answered_with_a_link_for_ten_minutes(fakes, monkeypatch) -> None:
    _dynamo, s3, _ = fakes(meeting=full_meeting(), parts=full_partitions())
    monkeypatch.setattr(index, "EXPORT_INLINE_BYTES", 1000)
    answer = mcp(mcp_identity("friend@example.com"))
    assert set(answer) == {"exportUrl", "expiresAt", "sizeBytes"}
    assert answer["exportUrl"].endswith("?expires=600")
    (key,) = s3.stored
    assert key.startswith(f"lma-meeting-archives/{POSIX}/exports/")
    assert key.endswith("-export.json")
    valid(json.loads(s3.stored[key]))
    assert answer["sizeBytes"] == len(s3.stored[key])


def test_the_size_is_measured_as_the_mcp_server_encodes_it(fakes) -> None:
    document = export_of(fakes)
    compact = len(json.dumps(document).encode())
    assert index.mcp_encoded_size(document) > compact


def test_a_refused_caller_gets_no_link_and_nothing_is_written(fakes, monkeypatch) -> None:
    _dynamo, s3, _ = fakes(meeting=full_meeting(), parts=full_partitions())
    monkeypatch.setattr(index, "EXPORT_INLINE_BYTES", 10)
    with pytest.raises(RuntimeError):
        mcp(mcp_identity("x@example.com"))
    assert s3.stored == {}


# ── the meeting page's download, and the archive ─────────────────────────────


def test_the_export_download_is_one_json_file_linked_for_ten_minutes(fakes) -> None:
    _dynamo, s3, lam = fakes(meeting=full_meeting(), parts=full_partitions())
    started = prepare(FRIEND, contents="EXPORT")
    assert started["FileName"].endswith("-export.json")
    build = json.loads(lam.invocations[0]["Payload"])
    index.handler(build, Context())
    ready = get(FRIEND, started["ArchiveId"])
    assert ready["Status"] == "READY"
    assert ready["DownloadUrl"].endswith("?expires=600")
    stored = next(v for k, v in s3.stored.items() if k.endswith("-export.json"))
    valid(json.loads(stored))


def test_an_export_that_cannot_be_built_is_left_out_of_the_archive_with_a_note(
    fakes, monkeypatch
) -> None:
    _dynamo, s3, lam = fakes(meeting=full_meeting(), parts=full_partitions())

    def broken(*_args, **_kwargs):
        raise ValueError("an unusual stored attribute")

    monkeypatch.setattr(index.export, "build_export", broken)
    prepare(OWNER)
    index.handler(json.loads(lam.invocations[0]["Payload"]), Context())
    archive = next(v for k, v in s3.stored.items() if k.endswith(".zip"))
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        names = opened.namelist()
        readme = opened.read("README.md").decode()
    assert "meeting-export.json" not in names
    assert "summary/summary.md" in names
    assert "**Meeting export**: it could not be prepared" in readme


def test_the_everything_archive_holds_the_export_and_lists_it(fakes) -> None:
    dynamo, s3, lam = fakes(meeting=full_meeting(), parts=full_partitions())
    started = prepare(OWNER)
    index.handler(json.loads(lam.invocations[0]["Payload"]), Context())
    archive = next(v for k, v in s3.stored.items() if k.endswith(".zip"))
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        valid(json.loads(opened.read("meeting-export.json")))
        assert "`meeting-export.json`" in opened.read("README.md").decode()
        board = opened.read("whiteboards/1-Plan.md").decode()
        assert board.startswith("# Plan\n") and "```mermaid\nflowchart TD\n  a-->b\n```" in board
        assert "`whiteboards/1-Plan.md`" in opened.read("README.md").decode()
    assert started["Contents"] == "EVERYTHING"
    # Only the whiteboards themselves are read, not their versions or counters.
    whiteboard_reads = [
        q for q in dynamo.queries if q["ExpressionAttributeValues"][":pk"]["S"].startswith("wb#")
    ]
    assert whiteboard_reads
    assert all(q["ExpressionAttributeValues"][":prefix"]["S"] == "b#" for q in whiteboard_reads)
