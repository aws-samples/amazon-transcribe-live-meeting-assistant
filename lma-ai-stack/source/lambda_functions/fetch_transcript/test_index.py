# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the transcript-assembly Lambda.

Every downstream consumer of a meeting's text goes through this function: the
summary prompts, the knowledge-base export and the meeting assistant all ask it
for the transcript. It is also documented as a customer extension point, so its
observable contract -- segment order, speaker attribution, which segments are
included, and how the token budget is applied -- is what a fork has to preserve.

The failure that matters most is ordering. Segments arrive on two channels and are
written as they are finalised, so the table returns them in no useful order; if the
sort were lost, the transcript would still look well-formed and would still
summarise, but the meeting would read as though people spoke out of turn. The tests
below therefore assert exact assembled strings rather than membership.

No AWS calls: the module builds its DynamoDB table handle at import time and that
handle is replaced with a recorder, which also lets the query expression itself be
asserted, since the channel and partial-segment filters are applied server-side.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TABLE_NAME = "lma-call-events"

# index.py reads the table name and builds boto3 clients at import time; client
# construction resolves an endpoint, which needs a region even though no call is
# ever made.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("LCA_CALL_EVENTS_TABLE", TABLE_NAME)

HERE = Path(__file__).resolve().parent


def _load_module():
    """Import this directory's index.py under a name of its own.

    Every Lambda source directory in this tree has an ``index.py``, so a plain
    ``import index`` resolves to whichever directory happens to come first on
    sys.path.
    """
    spec = importlib.util.spec_from_file_location("fetch_transcript_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_module()

CALL_ID = "2359fb61-f612-4fe9-bce2-839061c328f9"
METADATA = {
    "PK": f"c#{CALL_ID}",
    "SK": f"c#{CALL_ID}",
    "CallId": CALL_ID,
    "CreatedAt": "2025-01-02T03:04:05.000Z",
    "TotalConversationDurationMillis": Decimal("125400"),
}


def segment(transcript, end_time, channel="CALLER", speaker=None, **extra):
    """One finalised transcript segment, shaped as the table stores it.

    EndTime is a Decimal because that is what the DynamoDB resource type returns
    for a number, and Decimal is what the ordering has to cope with.
    """
    row = {
        "PK": f"trs#{CALL_ID}",
        "SK": f"{end_time}#{channel}",
        "Transcript": transcript,
        "EndTime": Decimal(str(end_time)),
        "Channel": channel,
        "IsPartial": False,
    }
    if speaker is not None:
        row["Speaker"] = speaker
    row.update(extra)
    return row


class FakeTable:
    """Stands in for the call-events table and records every request."""

    def __init__(
        self, items=None, metadata=None, query_error=None, metadata_error=None, page_size=None
    ):
        self.items = [] if items is None else items
        self.metadata = METADATA if metadata is None else metadata
        self.query_error = query_error
        self.metadata_error = metadata_error
        self.page_size = page_size
        self.queries = []
        self.lookups = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.query_error is not None:
            raise self.query_error
        # Page the way DynamoDB does when `page_size` is set: hand back a slice
        # plus a LastEvaluatedKey while rows remain. Without this a caller that
        # forgot to follow the key looks identical to one that follows it.
        rows = [dict(row) for row in self.items]
        if self.page_size is None:
            return {"Items": rows}
        start = 0
        if "ExclusiveStartKey" in kwargs:
            resume = kwargs["ExclusiveStartKey"]["SK"]
            start = next(i for i, r in enumerate(rows) if r["SK"] == resume) + 1
        page = rows[start:start + self.page_size]
        response = {"Items": page}
        if start + self.page_size < len(rows):
            response["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        return response

    def get_item(self, **kwargs):
        self.lookups.append(kwargs)
        if self.metadata_error is not None:
            raise self.metadata_error
        return {"Item": self.metadata}


def install(monkeypatch, **kwargs):
    table = FakeTable(**kwargs)
    monkeypatch.setattr(index, "ddbTable", table)
    return table


def run(monkeypatch, items=None, **event):
    """Invoke the handler for a meeting whose table holds ``items``."""
    table = install(monkeypatch, items=items)
    response = index.lambda_handler({"CallId": CALL_ID, **event}, None)
    return response, table


def table_error(code="ProvisionedThroughputExceededException"):
    return ClientError({"Error": {"Code": code, "Message": "slow down"}}, "Query")


# --------------------------------------------------------------------------
# Segment ordering
# --------------------------------------------------------------------------


def test_segments_are_assembled_in_end_time_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Segments finalise out of order across the two channels.

    A longer utterance on one channel is written after a short one that followed
    it, so the stored order is not the spoken order and the transcript has to be
    sorted before it is joined.
    """
    response, _ = run(
        monkeypatch,
        [
            segment("and that is the plan", 9, channel="CALLER"),
            segment("good morning", 1, channel="AGENT"),
            segment("what is the budget", 4, channel="CALLER"),
        ],
    )
    assert response["transcript"] == "\ngood morning\nwhat is the budget\nand that is the plan"


def test_segments_are_ordered_by_elapsed_time_and_not_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering is numeric: segment 10 comes after segment 9, not before it.

    Compared as strings, "10.5" sorts ahead of "9.5", which would silently
    reorder every meeting longer than ten seconds.
    """
    response, _ = run(
        monkeypatch,
        [
            segment("second", 10.5),
            segment("first", 9.5),
            segment("third", 100.25),
        ],
    )
    assert response["transcript"] == "\nfirst\nsecond\nthird"


def test_segments_sharing_an_end_time_keep_the_order_the_table_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two channels can finalise on the same timestamp; the sort must be stable.

    With an unstable sort, simultaneous speech would swap order between two
    otherwise identical requests, so a re-run of the summary would disagree with
    the transcript the user is reading.
    """
    response, _ = run(
        monkeypatch,
        [
            segment("spoken first", 5, channel="AGENT"),
            segment("spoken second", 5, channel="CALLER"),
        ],
    )
    assert response["transcript"] == "\nspoken first\nspoken second"


def test_a_segment_timestamped_at_zero_seconds_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first words of a meeting can carry EndTime 0, which is falsy.

    Anything that treats the timestamp as a truth value rather than a number drops
    the opening of the meeting.
    """
    response, _ = run(
        monkeypatch,
        [segment("later", 3), segment("the very first words", 0)],
    )
    assert response["transcript"] == "\nthe very first words\nlater"


def test_each_segment_starts_on_its_own_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn boundaries are newlines; without them the model reads one run-on turn."""
    response, _ = run(monkeypatch, [segment("one", 1), segment("two", 2)])
    assert response["transcript"].split("\n") == ["", "one", "two"]


# --------------------------------------------------------------------------
# Speaker attribution and channel labelling
# --------------------------------------------------------------------------


def test_each_segment_is_labelled_with_its_speaker_when_labels_are_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribution is what lets a summary assign an action item to a person."""
    response, _ = run(
        monkeypatch,
        [
            segment("I will send the deck", 1, speaker="Alice"),
            segment("thanks", 2, speaker="Bob"),
        ],
        IncludeSpeaker=True,
    )
    assert response["transcript"] == "\nAlice: I will send the deck\nBob: thanks"


def test_surrounding_whitespace_is_trimmed_from_the_speaker_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names arrive from meeting-platform rosters and are not always tidy."""
    response, _ = run(
        monkeypatch, [segment("hello", 1, speaker="  Alice Smith  ")], IncludeSpeaker=True
    )
    assert response["transcript"] == "\nAlice Smith: hello"


def test_a_segment_with_no_recorded_speaker_is_left_unlabelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better an unattributed line than an invented or empty attribution."""
    response, _ = run(
        monkeypatch,
        [segment("who said this", 1), segment("not me", 2, speaker="")],
        IncludeSpeaker=True,
    )
    assert response["transcript"] == "\nwho said this\nnot me"


def test_no_speaker_labels_are_added_unless_the_caller_asks_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that render their own attribution get the raw text."""
    response, _ = run(monkeypatch, [segment("hello", 1, speaker="Alice")], IncludeSpeaker=False)
    assert response["transcript"] == "\nhello"


def test_speaker_labels_are_off_unless_the_event_asks_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extension point is invoked with a bare CallId by some callers."""
    response, _ = run(monkeypatch, [segment("hello", 1, speaker="Alice")])
    assert response["transcript"] == "\nhello"


def test_assistant_replies_are_attributed_to_the_meeting_assistant() -> None:
    """Assistant turns stay in the transcript so follow-up questions have context.

    They are labelled by channel rather than by the Speaker field, so a stored
    speaker name does not relabel the assistant as a participant.
    """
    data = index.preprocess_transcripts(
        [
            segment("what did we decide", 1, channel="CALLER", speaker="Alice"),
            segment("You agreed to ship Friday.", 2, channel="AGENT_ASSISTANT", speaker="Bot"),
        ],
        False,
        True,
    )
    assert data == ["\nAlice: what did we decide", "\nMeetingAssistant: You agreed to ship Friday."]


# --------------------------------------------------------------------------
# Which segments are read from the table
# --------------------------------------------------------------------------


def test_only_final_segments_from_the_two_audio_channels_are_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial segments are revised in place as speech is recognised.

    Including them would repeat the same words several times in progressively
    longer forms, so the filter is applied in the query rather than after it.
    """
    table = install(monkeypatch, items=[segment("hello", 1)])
    index.get_transcripts(CALL_ID)
    assert len(table.queries) == 1
    assert table.queries[0]["FilterExpression"] == (
        (Attr("Channel").eq("AGENT") | Attr("Channel").eq("CALLER")) & Attr("IsPartial").eq(False)
    )


def test_segments_are_read_from_the_meetings_transcript_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = install(monkeypatch, items=[])
    index.get_transcripts(CALL_ID)
    assert table.queries[0]["KeyConditionExpression"] == Key("PK").eq(f"trs#{CALL_ID}")


def test_meeting_metadata_is_read_from_the_meetings_own_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata item is stored under a key whose parts are both "c#<callid>"."""
    table = install(monkeypatch)
    metadata = index.get_call_metadata(CALL_ID)
    assert table.lookups[0]["Key"] == {"PK": f"c#{CALL_ID}", "SK": f"c#{CALL_ID}"}
    assert metadata == METADATA


def test_metadata_is_returned_beside_the_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """The summary function reads CreatedAt and the duration from this payload."""
    response, _ = run(monkeypatch, [segment("hello", 1)])
    assert response["metadata"] == METADATA
    assert set(response) == {"transcript", "metadata"}


def test_a_table_error_while_reading_segments_is_raised_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttled read must not be reported as an empty meeting.

    Returning "" here would have the summary function store a confident summary of
    a meeting it never saw.
    """
    install(monkeypatch, query_error=table_error())
    with pytest.raises(ClientError):
        index.get_transcripts(CALL_ID)


def test_a_table_error_while_reading_metadata_is_raised_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, metadata_error=table_error("ResourceNotFoundException"))
    with pytest.raises(ClientError):
        index.get_call_metadata(CALL_ID)


# --------------------------------------------------------------------------
# Empty and single-segment meetings
# --------------------------------------------------------------------------


def test_a_meeting_with_no_final_segments_yields_an_empty_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meeting that nobody spoke in is a normal outcome, not an error."""
    response, _ = run(monkeypatch, [])
    assert response["transcript"] == ""
    assert response["metadata"] == METADATA


def test_a_single_segment_meeting_yields_just_that_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, _ = run(
        monkeypatch, [segment("we are done", 1, speaker="Alice")], IncludeSpeaker=True
    )
    assert response["transcript"] == "\nAlice: we are done"


# --------------------------------------------------------------------------
# Optional processing of the segment text
# --------------------------------------------------------------------------


def test_processing_strips_issue_markers_and_html() -> None:
    """The stored text carries UI markup that is meaningless to a model.

    Segments are rendered in the web UI, so they hold an issue-detected pill and
    other HTML; both reach the prompt verbatim unless processing is requested.
    """
    data = index.preprocess_transcripts(
        [segment("the <span class='issue-pill'>Issue Detected</span>budget <b>is</b> fine", 1)],
        True,
        False,
    )
    assert data == ["\nthe budget is fine"]


def test_processing_leaves_spoken_words_alone() -> None:
    """Condensing removes markup, not speech.

    Filler words stay: the models these transcripts are summarised by are
    untroubled by disfluent speech, and the pass that used to strip them
    corrupted real words ("umbrella" -> "brella") and deleted every
    meaning-bearing "like" along with the filler ones.
    """
    spoken = "Um, I would like the umbrella policy reviewed. Likewise the contract."
    assert index.preprocess_transcripts([segment(spoken, 1)], True, False) == [f"\n{spoken}"]


def test_processing_does_not_truncate_words_that_begin_with_a_filler() -> None:
    """The specific corruption the removed pass caused, pinned so it cannot return."""
    for word in ("umbrella", "Likewise", "umbrage", "uhlan", "likelihood"):
        assert word in index.preprocess_transcripts(
            [segment(f"the {word} matters", 1)], True, False
        )[0]


def test_segment_text_is_left_exactly_as_stored_when_processing_is_not_requested() -> None:
    """Default behaviour is verbatim, markup and all."""
    raw = "Um, the <b>budget</b> is fine"
    assert index.preprocess_transcripts([segment(raw, 1)], False, False) == [f"\n{raw}"]


def test_processing_is_off_unless_the_event_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, _ = run(monkeypatch, [segment("Um, the <b>budget</b>", 1)])
    assert response["transcript"] == "\nUm, the <b>budget</b>"


def test_a_segment_that_processes_away_to_nothing_adds_no_blank_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A segment of nothing but markup leaves no trace in the transcript.

    An empty line between two turns reads as a pause that did not happen, and
    wastes a token in every prompt.
    """
    data = index.preprocess_transcripts(
        [segment("<b></b>", 1), segment("the budget is fine", 2)], True, False
    )
    assert data == ["", "\nthe budget is fine"]
    assert "".join(data) == "\nthe budget is fine"


# --------------------------------------------------------------------------
# The token budget
# --------------------------------------------------------------------------


def test_the_transcript_is_returned_whole_when_no_token_count_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncation drops the end of the meeting, where decisions tend to land.

    Callers that fit the whole meeting in the model's context window omit the
    count, and zero means the same thing.
    """
    items = [segment("good morning everyone", 1), segment("we ship on Friday", 2)]
    whole = "\ngood morning everyone\nwe ship on Friday"
    without_count, _ = run(monkeypatch, items)
    with_zero, _ = run(monkeypatch, items, TokenCount=0)
    assert without_count["transcript"] == whole
    assert with_zero["transcript"] == whole


def test_a_token_count_keeps_the_start_of_the_meeting_and_drops_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget counts words, newlines and the spaces between them alike.

    The exact cut is asserted because it decides how much of the meeting a
    small-context model is told about: the first six tokens of this transcript are
    the leading newline, two words and the two spaces around them.
    """
    response, _ = run(
        monkeypatch,
        [segment("good morning everyone here", 1), segment("we ship on Friday", 2)],
        TokenCount=6,
    )
    assert response["transcript"] == "\ngood morning everyone"


def test_the_token_count_spans_the_whole_transcript_not_each_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is the model's context window, so it applies once, at the end."""
    response, _ = run(
        monkeypatch,
        [segment("alpha", 1), segment("beta", 2), segment("gamma", 3)],
        TokenCount=4,
    )
    assert response["transcript"] == "\nalpha\nbeta"


def test_a_token_count_larger_than_the_transcript_changes_nothing() -> None:
    assert index.truncate_number_of_words("\nalpha\nbeta", 1000) == "\nalpha\nbeta"


def test_truncation_preserves_the_newlines_inside_the_kept_text() -> None:
    """Turn boundaries have to survive the cut, or the tail reads as one turn."""
    assert index.truncate_number_of_words("\nalpha\nbeta\ngamma", 4) == "\nalpha\nbeta"


# --------------------------------------------------------------------------
# The event contract
# --------------------------------------------------------------------------


def test_the_meeting_is_identified_by_the_call_id_in_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = install(monkeypatch, items=[])
    index.lambda_handler({"CallId": "another-meeting"}, None)
    assert table.queries[0]["KeyConditionExpression"] == Key("PK").eq("trs#another-meeting")
    assert table.lookups[0]["Key"]["PK"] == "c#another-meeting"


def test_the_event_must_name_a_meeting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assembling a transcript for an unnamed meeting has no correct answer."""
    install(monkeypatch, items=[])
    with pytest.raises(KeyError):
        index.lambda_handler({"TokenCount": 0}, None)


def test_the_stored_segments_are_read_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One query and one metadata read per invocation.

    This function is called for every summary section and by the assistant on each
    turn, so a repeated read multiplies table cost and latency per meeting.
    """
    _, table = run(monkeypatch, [segment("hello", 1), segment("again", 2)])
    assert len(table.queries) == 1
    assert len(table.lookups) == 1


# --------------------------------------------------------------------------
# Reading every page
#
# DynamoDB caps a Query response at 1 MB of items *read* and applies
# FilterExpression only afterwards. Partial segments are filtered here and are
# numerous, so they consume that budget without appearing in the result -- which
# means even a moderately long meeting spans several pages. A caller that reads
# only the first page returns a truncated transcript with nothing to indicate it,
# and the summary, the knowledge-base export and the assistant then each describe
# a partial meeting as if it were the whole one.
# --------------------------------------------------------------------------


def test_segments_from_every_page_are_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twelve segments over pages of five must all reach the transcript."""
    segments = [segment(f"line {i}", i) for i in range(1, 13)]
    table = install(monkeypatch, items=segments, page_size=5)
    result = index.lambda_handler({"CallId": CALL_ID}, None)
    for i in range(1, 13):
        assert f"line {i}" in result["transcript"], f"line {i} missing from the transcript"
    assert len(table.queries) == 3


def test_paging_follows_the_key_the_table_hands_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each continuation must resume from the last row of the previous page."""
    segments = [segment(f"line {i}", i) for i in range(1, 8)]
    table = install(monkeypatch, items=segments, page_size=3)
    index.lambda_handler({"CallId": CALL_ID}, None)
    resumed = [q["ExclusiveStartKey"]["SK"] for q in table.queries if "ExclusiveStartKey" in q]
    assert resumed == [f"{3}#CALLER", f"{6}#CALLER"]


def test_no_segment_is_returned_twice_when_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume key that is off by one would repeat or drop a line."""
    segments = [segment(f"line {i}", i) for i in range(1, 10)]
    install(monkeypatch, items=segments, page_size=4)
    transcript = index.lambda_handler({"CallId": CALL_ID}, None)["transcript"]
    for i in range(1, 10):
        assert transcript.count(f"line {i}") == 1, f"line {i} appears more than once"


def test_a_single_page_meeting_issues_one_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case must not pay for a second round trip."""
    table = install(monkeypatch, items=[segment("only", 1)], page_size=5)
    index.lambda_handler({"CallId": CALL_ID}, None)
    assert len(table.queries) == 1
    assert "ExclusiveStartKey" not in table.queries[0]


def test_the_filter_is_reapplied_on_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A continuation that dropped the filter would admit partial segments."""
    segments = [segment(f"line {i}", i) for i in range(1, 8)]
    table = install(monkeypatch, items=segments, page_size=3)
    index.lambda_handler({"CallId": CALL_ID}, None)
    assert len(table.queries) > 1
    for query in table.queries:
        assert "FilterExpression" in query
        assert query["FilterExpression"] == table.queries[0]["FilterExpression"]
        assert query["KeyConditionExpression"] == table.queries[0]["KeyConditionExpression"]


def test_a_repeated_resume_key_does_not_loop_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """A table handing back the same continuation key must stop the read.

    Found by mutation testing: removing the resume-key assignment made every
    query return page one with a continuation key still set, and the loop spun
    until the Lambda timed out. A key identical to the one just used cannot make
    progress, so it ends the read instead.
    """

    class StuckTable(FakeTable):
        """Always reports more to come, always from the same key."""

        def query(self, **kwargs):
            self.queries.append(kwargs)
            row = dict(self.items[0])
            return {"Items": [row], "LastEvaluatedKey": {"PK": row["PK"], "SK": row["SK"]}}

    table = StuckTable(items=[segment("stuck", 1)])
    monkeypatch.setattr(index, "ddbTable", table)
    index.lambda_handler({"CallId": CALL_ID}, None)
    # Two queries: the first page, then one attempt that repeats the key.
    assert len(table.queries) == 2


def test_the_read_is_bounded_by_a_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with an always-advancing key, the read cannot run without limit."""

    class EndlessTable(FakeTable):
        """Hands back a fresh key every time, so the read never ends on its own."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.page = 0

        def query(self, **kwargs):
            self.queries.append(kwargs)
            self.page += 1
            return {
                "Items": [segment(f"line {self.page}", self.page)],
                "LastEvaluatedKey": {"PK": f"trs#{CALL_ID}", "SK": f"{self.page}#CALLER"},
            }

    table = EndlessTable()
    monkeypatch.setattr(index, "ddbTable", table)
    index.lambda_handler({"CallId": CALL_ID}, None)
    assert len(table.queries) == index.MAX_TRANSCRIPT_PAGES


def test_markup_at_the_start_of_a_segment_leaves_no_leading_space() -> None:
    """Stripping markup can expose whitespace that was behind it.

    Each segment is prefixed with a newline, so a surviving leading space would
    indent that turn in every prompt. Mutation testing found this unasserted:
    removing the strip() changed nothing any test could see.
    """
    data = index.preprocess_transcripts(
        [segment("<span class='issue-pill'>Issue Detected</span> the budget is fine", 1)],
        True,
        False,
    )
    assert data == ["\nthe budget is fine"]
    assert not data[0].startswith("\n ")
