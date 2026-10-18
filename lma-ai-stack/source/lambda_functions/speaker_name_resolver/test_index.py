# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the speaker-name resolver's plumbing.

The judgement is tested in test_pure.py. What is tested here is the behaviour a
caller depends on: that a user who may not edit a meeting is refused, that a
failing model call cannot stop a meeting from ending, that nothing is written
when nothing was inferred, and that the names are written through AppSync rather
than straight to the table (which is what makes an open meeting page re-label
without being reloaded).

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

TABLE = "lma-call-events"
APPSYNC_URL = "https://example.appsync-api.us-east-1.amazonaws.com/graphql"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", TABLE)
os.environ.setdefault("APPSYNC_GRAPHQL_URL", APPSYNC_URL)
os.environ.setdefault("BEDROCK_MODEL_ID", "test.model")

HERE = Path(__file__).resolve().parent


def _load():
    """Import this directory's index.py under a name of its own.

    Every Lambda source directory in this tree has an ``index.py``, so a plain
    ``import index`` resolves to whichever directory comes first on sys.path.
    """
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("speaker_name_resolver_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

CALL_ID = "meeting-1"
OWNER = "owner@example.com"
SPK0 = "CALLER#Other Participant (spk_0)"
SPK1 = "CALLER#Other Participant (spk_1)"

MEETING_ROW = {
    "PK": {"S": f"c#{CALL_ID}"},
    "SK": {"S": f"c#{CALL_ID}"},
    "CallId": {"S": CALL_ID},
    "Owner": {"S": OWNER},
}


def segment_row(speaker, start, text, channel="CALLER", segment_id=None):
    return {
        "PK": {"S": f"trs#{CALL_ID}"},
        "SK": {"S": f"{start}#{channel}"},
        "SegmentId": {"S": segment_id or f"s{start}"},
        "Channel": {"S": channel},
        "Speaker": {"S": speaker},
        "Transcript": {"S": text},
        "StartTime": {"N": str(start)},
        "EndTime": {"N": str(start + 3)},
        "IsPartial": {"BOOL": False},
    }


SEGMENTS = [
    segment_row(
        "Other Participant (spk_0)", 0, "Hi everyone, I'm Sarah Lee, I run the platform team"
    ),
    segment_row("Other Participant (spk_1)", 5, "And I'm Priya Raman, I own data ingestion"),
]

MODEL_ANSWER = json.dumps(
    [
        {
            "label": SPK0,
            "name": "Sarah Lee",
            "basis": "self-introduction",
            "turn": 1,
            "quote": "I'm Sarah Lee",
        },
        {
            "label": SPK1,
            "name": "Priya Raman",
            "basis": "self-introduction",
            "turn": 2,
            "quote": "I'm Priya Raman",
        },
    ]
)


class FakeDynamo:
    """Stands in for the call-events table."""

    def __init__(self, meeting=None, segments=None, pages=None):
        self.meeting = MEETING_ROW if meeting is None else meeting
        self.segments = SEGMENTS if segments is None else segments
        self.pages = pages
        self.queries = []

    def get_item(self, **kwargs):  # noqa: D102
        return {"Item": self.meeting} if self.meeting else {}

    def query(self, **kwargs):  # noqa: D102
        self.queries.append(kwargs)
        rows = [dict(r) for r in self.segments]
        if self.pages is None:
            return {"Items": rows}
        start = 0
        if "ExclusiveStartKey" in kwargs:
            resume = kwargs["ExclusiveStartKey"]["SK"]["S"]
            start = next(i for i, r in enumerate(rows) if r["SK"]["S"] == resume) + 1
        page = rows[start : start + self.pages]
        out = {"Items": page}
        if start + self.pages < len(rows):
            out["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        return out


class FakeBedrock:
    def __init__(self, answer=MODEL_ANSWER, error=None):
        self.answer = answer
        self.error = error
        self.prompts = []

    def converse(self, **kwargs):  # noqa: D102
        self.prompts.append(kwargs["messages"][0]["content"][0]["text"])
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.answer}]}}}


def install(monkeypatch, meeting=None, segments=None, answer=MODEL_ANSWER, error=None, pages=None):
    ddb = FakeDynamo(meeting=meeting, segments=segments, pages=pages)
    bedrock = FakeBedrock(answer=answer, error=error)
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", bedrock)
    monkeypatch.setattr(
        index, "write_identities", lambda call_id, identities: written.append((call_id, identities))
    )
    return ddb, bedrock, written


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_self_introductions_are_named_and_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, written = install(monkeypatch)
    result = index.handler({"CallId": CALL_ID}, None)
    assert result == {"Success": True, "Message": "ok", "NamesApplied": 2}
    (call_id, identities) = written[0]
    assert call_id == CALL_ID
    assert identities["identities"][SPK0]["name"] == "Sarah Lee"
    assert identities["identities"][SPK0]["status"] == "confirmed"
    assert identities["identities"][SPK1]["name"] == "Priya Raman"


def test_the_prompt_carries_the_transcript_and_the_open_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, bedrock, _ = install(monkeypatch)
    index.handler({"CallId": CALL_ID}, None)
    prompt = bedrock.prompts[0]
    assert "I'm Sarah Lee" in prompt
    assert SPK0 in prompt


def test_segments_are_read_from_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long meeting spreads over several 1 MB pages; stopping at the first
    would hide the introductions if they were not on it."""
    many = [segment_row("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(6)]
    many[0] = segment_row("Other Participant (spk_0)", 0, "Hi, I'm Sarah Lee")
    ddb, _, _ = install(monkeypatch, segments=many, pages=2)
    index.handler({"CallId": CALL_ID}, None)
    assert len(ddb.queries) == 3


# --------------------------------------------------------------------------
# When nothing should happen
# --------------------------------------------------------------------------


def test_no_names_are_stored_when_the_model_names_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that infers nothing stores no names -- but it does record that it
    ran, so the cooldown holds. Otherwise the cheapest way to spend money would
    be to ask repeatedly about a meeting that has no introductions in it."""
    _, _, written = install(monkeypatch, answer="[]")
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["NamesApplied"] == 0
    assert len(written) == 1
    _, stored = written[0]
    assert stored["identities"] == {}
    assert stored["lastPass"]["turns"] == 2


def test_a_transcript_with_no_naming_language_does_not_call_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet = [
        segment_row("Other Participant (spk_0)", 0, "the numbers moved three percent"),
        segment_row("Other Participant (spk_1)", 5, "look at the second chart"),
    ]
    _, bedrock, written = install(monkeypatch, segments=quiet)
    result = index.handler({"CallId": CALL_ID}, None)
    assert bedrock.prompts == []
    assert written == []
    assert result["Message"] == "nothing to name"


def test_an_already_named_meeting_does_not_call_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    named = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {
                    "identities": {
                        SPK0: {"name": "Sarah Lee", "status": "confirmed"},
                        SPK1: {"name": "Priya Raman", "status": "confirmed"},
                    }
                }
            )
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=named)
    index.handler({"CallId": CALL_ID}, None)
    assert bedrock.prompts == []


def test_a_meeting_that_does_not_exist_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, meeting={})
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["Success"] is False
    assert "no such meeting" in result["Message"]


def test_an_event_with_no_call_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    with pytest.raises(ValueError):
        index.handler({}, None)


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


def test_the_owner_may_run_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, written = install(monkeypatch)
    event = {"arguments": {"input": {"CallId": CALL_ID}}, "identity": {"username": OWNER}}
    assert index.handler(event, None)["Success"] is True
    assert written


def test_a_user_who_does_not_own_the_meeting_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Lambda resolver has no DynamoDB condition to lean on, so the rule the
    direct-write resolver enforces has to be applied here instead."""
    _, bedrock, written = install(monkeypatch)
    event = {
        "arguments": {"input": {"CallId": CALL_ID}},
        "identity": {"username": "stranger@example.com", "groups": []},
    }
    result = index.handler(event, None)
    assert result["Success"] is False
    assert result["Message"] == "not permitted"
    assert written == []
    assert bedrock.prompts == []


def test_an_admin_may_run_a_pass_on_any_meeting(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, written = install(monkeypatch)
    event = {
        "arguments": {"input": {"CallId": CALL_ID}},
        "identity": {"username": "admin@example.com", "groups": ["Admin"]},
    }
    assert index.handler(event, None)["Success"] is True
    assert written


def test_a_user_the_meeting_is_shared_with_may_run_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for shared_with in ({"L": [{"S": "friend@example.com"}]}, {"S": "a@b.com,friend@example.com"}):
        shared = {**MEETING_ROW, "SharedWith": shared_with}
        _, _, written = install(monkeypatch, meeting=shared)
        event = {
            "arguments": {"input": {"CallId": CALL_ID}},
            "identity": {"username": "friend@example.com", "groups": []},
        }
        assert index.handler(event, None)["Success"] is True, shared_with
        assert written


def test_a_backend_invoke_has_no_identity_and_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-of-meeting pass is the backend calling itself."""
    _, _, written = install(monkeypatch)
    assert index.handler({"CallId": CALL_ID}, None)["Success"] is True
    assert written


# --------------------------------------------------------------------------
# Failure containment
# --------------------------------------------------------------------------


def test_a_failing_model_call_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """This runs just before a meeting is closed out. A meeting must not fail to
    end because a model call did -- naming is an enhancement, ending is not.
    """
    _, _, written = install(monkeypatch, error=RuntimeError("bedrock unavailable"))
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["Success"] is False
    # The service's own error text is logged, not returned: it would otherwise
    # put DynamoDB and AppSync internals in a browser.
    assert "bedrock" not in result["Message"]
    assert result["Message"] == "could not identify speakers just now"
    assert written == []


def test_a_failing_write_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("appsync rejected the write")

    monkeypatch.setattr(index, "write_identities", explode)
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["Success"] is False
    assert "appsync" not in result["Message"]


def test_a_model_answer_that_is_not_json_names_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, written = install(monkeypatch, answer="I could not work out who anyone is.")
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["NamesApplied"] == 0
    assert written[0][1]["identities"] == {}


def test_a_model_naming_a_label_that_does_not_exist_names_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = json.dumps(
        [
            {
                "label": "CALLER#Ghost (spk_9)",
                "name": "Ghost",
                "basis": "self-introduction",
                "turn": 1,
            }
        ]
    )
    _, _, written = install(monkeypatch, answer=hostile)
    assert index.handler({"CallId": CALL_ID}, None)["NamesApplied"] == 0
    assert written[0][1]["identities"] == {}


# --------------------------------------------------------------------------
# On-demand behaviour
# --------------------------------------------------------------------------


def test_the_button_overrides_the_shortcut_but_an_automatic_pass_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet = [
        segment_row("Other Participant (spk_0)", 0, "the numbers moved three percent"),
        segment_row("Other Participant (spk_1)", 5, "look at the second chart"),
    ]
    _, bedrock, _ = install(monkeypatch, segments=quiet, answer="[]")
    index.handler({"CallId": CALL_ID, "Force": True}, None)
    assert bedrock.prompts == [], "a backend invoke must not be able to force a model call"

    _, bedrock2, _ = install(monkeypatch, segments=quiet, answer="[]")
    event = {
        "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
        "identity": {"username": OWNER},
    }
    index.handler(event, None)
    assert len(bedrock2.prompts) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------
# Bounding what a client can spend
# --------------------------------------------------------------------------


def test_a_pass_that_ran_moments_ago_is_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timer driving automatic passes lives in the browser, and a client
    cannot be trusted to pace a paid model call -- several viewers each run their
    own, and the mutation can simply be called in a loop."""
    recent = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps({"identities": {}, "lastPass": {"at": _now_iso(), "turns": 1}})
        },
    }
    _, bedrock, written = install(monkeypatch, meeting=recent)
    result = index.handler({"CallId": CALL_ID, "Force": True}, None)
    assert bedrock.prompts == []
    assert written == []
    assert "recently" in result["Message"]


def test_the_button_cannot_bypass_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing the button overrides the "nothing here could name anybody"
    shortcut, not the cost bound."""
    recent = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps({"identities": {}, "lastPass": {"at": _now_iso(), "turns": 1}})
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=recent)
    event = {
        "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
        "identity": {"username": OWNER},
    }
    index.handler(event, None)
    assert bedrock.prompts == []


def test_a_pass_is_declined_when_nobody_has_spoken_since_the_last_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cooldown has elapsed, but the transcript is the same, so another pass
    provably cannot learn anything."""
    stale = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 2}}
            )
        },
    }
    _, bedrock, written = install(monkeypatch, meeting=stale)
    result = index.handler({"CallId": CALL_ID, "Force": True}, None)
    assert bedrock.prompts == []
    assert written == []
    assert "nothing new" in result["Message"]


def test_a_pass_runs_once_the_transcript_has_grown(monkeypatch: pytest.MonkeyPatch) -> None:
    grown = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 1}}
            )
        },
    }
    _, bedrock, written = install(monkeypatch, meeting=grown)
    assert index.handler({"CallId": CALL_ID}, None)["NamesApplied"] == 2
    assert len(bedrock.prompts) == 1
    assert written[0][1]["lastPass"]["turns"] == 2


def test_a_malformed_last_pass_stamp_does_not_block_naming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for stamp in ('{"lastPass": "nonsense"}', '{"lastPass": {"at": "not a date"}}', "{}"):
        odd = {**MEETING_ROW, "SpeakerIdentities": {"S": stamp}}
        _, bedrock, _ = install(monkeypatch, meeting=odd)
        index.handler({"CallId": CALL_ID}, None)
        assert len(bedrock.prompts) == 1, stamp


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# The end-of-meeting pass
# --------------------------------------------------------------------------


def _long_meeting(count=300):
    return [segment_row("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(count)]


def test_a_long_meeting_does_not_silence_naming_for_ever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The growth check once compared the TRIMMED list against a stamp taken from
    it, and both saturate at the trim limit -- so past it every later pass, the
    button and the end-of-meeting pass were all declined permanently."""
    rows = _long_meeting()
    rows[0] = segment_row("Other Participant (spk_0)", 0, "Hi, I'm Sarah Lee")
    stamped = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                # A stamp from a previous pass, at the trim limit.
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 160}}
            )
        },
    }
    _, bedrock, written = install(monkeypatch, meeting=stamped, segments=rows)
    index.handler({"CallId": CALL_ID}, None)
    assert len(bedrock.prompts) == 1, "a 300-turn meeting must still be read"
    assert written[0][1]["lastPass"]["turns"] == 300, "the stamp records the WHOLE transcript"


def test_the_end_of_meeting_pass_is_not_declined_by_the_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It runs once per meeting, is not client-driven, and is the pass the
    summary depends on -- so a browser timer firing 20 seconds earlier must not
    lose the names from the summary."""
    recent = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps({"identities": {}, "lastPass": {"at": _now_iso(), "turns": 2}})
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=recent)
    index.handler({"CallId": CALL_ID, "Trigger": "meeting-end"}, None)
    assert len(bedrock.prompts) == 1


def test_the_end_of_meeting_pass_runs_even_if_nobody_spoke_since_the_last_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unchanged = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 2}}
            )
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=unchanged)
    index.handler({"CallId": CALL_ID, "Trigger": "meeting-end"}, None)
    assert len(bedrock.prompts) == 1


def test_a_browser_caller_cannot_claim_to_be_the_end_of_meeting_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the cooldown would be one JSON field away from meaningless."""
    recent = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps({"identities": {}, "lastPass": {"at": _now_iso(), "turns": 2}})
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=recent)
    index.handler(
        {
            "info": {"fieldName": "resolveSpeakerNames"},
            "arguments": {"input": {"CallId": CALL_ID, "Force": True, "Trigger": "meeting-end"}},
            "identity": {"username": OWNER},
            "Trigger": "meeting-end",
        },
        None,
    )
    assert bedrock.prompts == []


def test_a_pass_whose_names_were_all_superseded_still_records_that_it_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It cost a model call either way, so leaving no stamp would let the next
    request pay for another immediately."""
    # Both voices are renamed by a person while the pass is in flight, so every
    # inferred name loses the merge.
    install(monkeypatch)
    ddb = FakeDynamo(meeting=MEETING_ROW)
    renamed_during_the_pass = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {
                    "identities": {
                        SPK0: {"name": "Alice", "status": "confirmed", "source": "manual"},
                        SPK1: {"name": "Bob", "status": "confirmed", "source": "manual"},
                    }
                }
            )
        },
    }
    reads = {"n": 0}

    def get_item(**kwargs):
        # First read is before the model call; the second is the re-read just
        # before the write, by which time the names have been typed.
        reads["n"] += 1
        return {"Item": MEETING_ROW if reads["n"] == 1 else renamed_during_the_pass}

    ddb.get_item = get_item
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", FakeBedrock())
    monkeypatch.setattr(index, "write_identities", lambda c, i: written.append((c, i)))

    result = index.handler({"CallId": CALL_ID}, None)
    assert result["NamesApplied"] == 2, "the pass did infer names"
    stored = written[-1][1]
    assert stored["identities"][SPK0]["name"] == "Alice", "the typed name wins"
    assert stored["lastPass"]["turns"] == 2, "and the pass is still stamped"


def test_the_button_still_works_on_an_ended_meeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ended meeting's transcript can never grow again, so without a bypass
    every press after the first pass was declined for ever -- including the
    documented recovery of pasting an attendee list and asking again, which is
    exactly when the button matters most."""
    stamped_long_ago = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 2}}
            )
        },
    }
    _, bedrock, written = install(monkeypatch, meeting=stamped_long_ago)
    event = {
        "info": {"fieldName": "resolveSpeakerNames"},
        "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
        "identity": {"username": OWNER},
    }
    result = index.handler(event, None)
    assert len(bedrock.prompts) == 1, "the button must reach the model"
    assert result["NamesApplied"] == 2
    assert written


def test_an_automatic_pass_on_an_ended_meeting_is_still_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a person pressing the button bypasses the growth gate; the timer
    must not, or a page left open would pay for a pass every 45 seconds for
    nothing."""
    stamped_long_ago = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 2}}
            )
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=stamped_long_ago)
    event = {
        "info": {"fieldName": "resolveSpeakerNames"},
        "arguments": {"input": {"CallId": CALL_ID, "Force": False}},
        "identity": {"username": OWNER},
    }
    index.handler(event, None)
    assert bedrock.prompts == []


def test_the_button_is_still_subject_to_the_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So the bypass is not a way to spend without limit."""
    just_ran = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps({"identities": {}, "lastPass": {"at": _now_iso(), "turns": 2}})
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=just_ran)
    event = {
        "info": {"fieldName": "resolveSpeakerNames"},
        "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
        "identity": {"username": OWNER},
    }
    index.handler(event, None)
    assert bedrock.prompts == []
