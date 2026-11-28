# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Fact checks and lookups: the candidates a run reports and what is published from a finding.

The plumbing (the research lease, the request, the cap shared with the rest of
the guidance) is tested in test_checks_index.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))

import checks  # noqa: E402
import pure  # noqa: E402

RECORDED = json.loads((HERE / "fixtures" / "renewal_meeting.json").read_text(encoding="utf-8"))
SEGMENTS = pure.final_segments(RECORDED["segments"])
_TEXT, NAMES = pure.transcript_block(SEGMENTS)
BY_ID = {segment.segment_id: name for name, segment in NAMES.items()}


def entry(**fields):
    value = {
        "kind": "fact_check",
        "subject": "renewal price",
        "claim": "The renewal is $42,000 a year on a two-year term.",
        "segment": BY_ID["seg-04"],
        "quote": "forty two thousand a year on a two year term",
        "why": "The price decision rests on it.",
        "confidence": 0.9,
    }
    value.update(fields)
    return value


def parse(entries, kinds=("fact_check", "lookup"), threshold=0.7):
    return checks.parse_checks(entries, NAMES, kinds, threshold, now=1000.0)


# ── candidates ───────────────────────────────────────────────────────────────


def test_a_candidate_is_kept_with_its_segment_speaker_and_key():
    (check,), drops = parse([entry()])
    assert drops == []
    assert check.key == "fact_check:claim:renewal-price"
    assert (check.segment_id, check.speaker, check.start) == ("seg-04", "Lee", 30.0)
    assert check.found_at == 1000.0


def test_a_lookup_is_keyed_as_a_term():
    (check,), _ = parse(
        [
            entry(
                kind="lookup",
                subject="Extended support hours",
                claim="extended support hours",
                quote="extended support hours",
                segment=BY_ID["seg-06"],
            )
        ]
    )
    assert check.key == "lookup:term:extended-support-hours"


def test_a_candidate_must_quote_the_segment_it_cites():
    kept, drops = parse([entry(quote="forty five thousand a year")])
    assert kept == [] and [d.reason for d in drops] == [checks.REASON_QUOTE]
    assert drops[0].subject == "renewal price" and drops[0].kind == "fact_check"


def test_a_candidate_must_cite_a_segment_the_run_was_shown():
    kept, drops = parse([entry(segment="S999")])
    assert kept == [] and [d.reason for d in drops] == [checks.REASON_SEGMENT]


def test_only_the_kinds_that_are_on_are_kept():
    kept, drops = parse([entry()], kinds=("lookup",))
    assert kept == [] and [d.reason for d in drops] == [checks.REASON_KIND]


def test_a_candidate_below_the_threshold_or_without_a_subject_is_dropped():
    kept, drops = parse([entry(confidence=0.5), entry(subject="  !! "), entry(claim="")])
    assert kept == [] and [d.reason for d in drops] == [
        checks.REASON_THRESHOLD,
        checks.REASON_INCOMPLETE,
        checks.REASON_INCOMPLETE,
    ]


def test_at_most_five_candidates_are_kept_from_a_run_and_one_per_key():
    entries = [entry()] + [entry(subject=f"claim {n}") for n in range(7)]
    entries.insert(1, entry())
    kept, drops = parse(entries)
    assert [c.subject for c in kept] == [
        "renewal price",
        "claim 0",
        "claim 1",
        "claim 2",
        "claim 3",
    ]
    assert [d.reason for d in drops] == [checks.REASON_DUPLICATE] + [checks.REASON_PER_ANSWER] * 3


def test_a_check_survives_its_record():
    (check,), _ = parse([entry()])
    assert checks.Check.from_record(json.loads(json.dumps(check.to_record()))) == check
    assert checks.Check.from_record({"kind": "delete"}) is None


# ── dedup ────────────────────────────────────────────────────────────────────


def test_claims_with_the_same_content_words_are_the_same_claim():
    assert checks.similar(
        "Lambda functions time out after 5 minutes",
        "AWS Lambda functions time out after 5 minutes at most",
    )
    assert not checks.similar("Lambda functions time out after 5 minutes", "S3 buckets are global")
    assert not checks.similar("", "anything")


def test_a_candidate_already_checked_or_waiting_is_not_checked_again():
    (first,), _ = parse([entry()])
    (reworded,), _ = parse(
        [
            entry(
                subject="price of the renewal",
                claim="The renewal is $42,000 a year on a two-year term!",
            )
        ]
    )
    (other,), _ = parse(
        [
            entry(
                subject="billing",
                claim="Billing is annual, invoiced in January.",
                segment=BY_ID["seg-18"],
                quote="annual it is, invoiced in January",
            )
        ]
    )
    checked = checks.remember({}, first, 1000.0, "contradicted", "n1")
    assert checks.fresh([first, reworded, other], checked, []) == [other]
    assert checks.fresh([first, other], {}, [first]) == [other]


def _term(n):
    return checks.Check(
        "lookup", f"t{n}", f"term number {n}", "q", "s", "A", 0, "", 0.9, f"lookup:term:t{n}"
    )


def test_the_queue_admits_what_fits_and_logs_why_the_rest_is_dropped():
    waiting = [_term(n) for n in range(8)]
    kept, drops = checks.admit([_term(n) for n in range(8, 12)], {}, waiting, 100, 5.0)
    assert [c.subject for c in kept] == ["t8", "t9"]
    assert [(d.subject, d.reason) for d in drops] == [
        ("t10", checks.REASON_QUEUE_FULL),
        ("t11", checks.REASON_QUEUE_FULL),
    ]
    assert drops[0].at == 5.0


def test_the_queue_respects_the_meeting_cap():
    kept, drops = checks.admit([_term(1), _term(2), _term(3)], {}, [], 1, 5.0)
    assert [c.subject for c in kept] == ["t1"]
    assert {d.reason for d in drops} == {checks.REASON_MEETING_CAP}


def test_a_repeat_is_dropped_as_a_duplicate_or_as_similar_wording():
    checked = checks.remember({}, _term(1), 1.0, "answered")
    reworded = checks.Check(
        "lookup", "other", "term number 1", "q", "s", "A", 0, "", 0.9, "lookup:term:other"
    )
    _kept, drops = checks.admit([_term(1), reworded], checked, [], 100, 5.0)
    assert [d.reason for d in drops] == [checks.REASON_DUPLICATE, checks.REASON_SIMILAR]
    assert checks.drop_counts(drops) == {checks.REASON_DUPLICATE: 1, checks.REASON_SIMILAR: 1}


def test_dismissed_items_mark_their_candidate_and_are_named_to_the_run():
    (check,), _ = parse([entry()])
    checked = checks.remember({}, check, 1000.0, "contradicted", "n1")
    assert checks.mark_dismissed(checked, ["n9"]) == 0
    assert checks.mark_dismissed(checked, ["n1"]) == 1
    assert checks.mark_dismissed(checked, ["n1"]) == 0
    # A later record of the same candidate keeps the dismissal.
    checked = checks.remember(checked, check, 1100.0, "contradicted", None)
    assert checked[check.key]["dismissed"] is True
    assert "(dismissed: report nothing like it)" in checks.render_done(checked, [])


def test_the_checks_done_list_is_bounded():
    checked = {}
    for n in range(50):
        check = checks.Check(
            "lookup", f"t{n}", f"term {n}", "q", "s", "A", 0, "", 0.9, f"lookup:term:t{n}"
        )
        checked = checks.remember(checked, check, float(n), "answered")
    lines = checks.render_done(checked, []).splitlines()
    assert len(lines) == checks.MAX_SHOWN_DONE and "term 49" in lines[0]
    assert checks.render_done({}, []) == "(none yet)"


def test_a_research_candidate_carries_the_transcript_around_it():
    (check,), _ = parse([entry()])
    (candidate,) = checks.research_candidates([check], SEGMENTS)
    assert candidate["id"] == "C1" and candidate["speaker"] == "Lee"
    lines = candidate["context"].splitlines()
    assert lines[0].startswith("[0:05] Dana:") and lines[-1].startswith("[0:39] Dana:")
    assert len(lines) == checks.CONTEXT_BEFORE + 1 + checks.CONTEXT_AFTER


# ── what is published ────────────────────────────────────────────────────────

SOURCES = {
    "R1": {
        "title": "Support price list 2026",
        "url": "https://example.com/prices",
        "via": "Documents",
    },
    "R2": {"title": "contract.pdf", "url": "", "via": "Documents"},
}


def finding(**fields):
    value = {
        "id": "C1",
        "verdict": "contradicted",
        "confidence": 0.9,
        "summary": "The 2026 price list gives $46,000 a year for this scope on a two-year term.",
        "sources": ["R1", "R2"],
    }
    value.update(fields)
    return value


def publish(found=None, **kwargs):
    (check,), _ = parse([entry()])
    return checks.publication(check, found or finding(), SOURCES, 0.75, **kwargs)


def test_a_contradicted_claim_is_published_as_possibly_inaccurate():
    item = publish()
    assert item.type == "fact_check" and item.severity == "warning"
    assert item.title == "Possibly inaccurate: renewal price"
    assert item.dedup_key == "fact_check:claim:renewal-price"
    assert item.refs == ["segment:seg-04"]
    assert "> “forty two thousand a year on a two year term” — Lee, 0:30" in item.body
    assert "**What the sources say:** The 2026 price list gives $46,000" in item.body
    assert "[Support price list 2026](<https://example.com/prices>) (Documents)" in item.body
    assert "contract.pdf (Documents)" in item.body
    assert "Confidence: high (0.90)" in item.body
    assert item.actions()[0]["kind"] == "copy"
    assert "https://example.com/prices" in item.actions()[0]["payload"]["text"]


def test_a_qualified_claim_is_published_as_worth_checking():
    item = publish(finding(verdict="qualified", confidence=0.8))
    assert item.title.startswith("Worth checking:") and item.severity == "suggestion"
    assert "Confidence: medium (0.80)" in item.body


def test_a_supported_claim_is_published_only_when_asked_for():
    assert publish(finding(verdict="supported")) is None
    item = publish(finding(verdict="supported"), show_supported=True)
    assert item.title.startswith("Consistent with sources:") and item.severity == "info"


def test_nothing_is_published_unverified_below_the_threshold_or_without_a_source():
    assert publish(finding(verdict="unverified")) is None
    assert publish(finding(confidence=0.7)) is None
    assert publish(finding(sources=["R7"])) is None
    assert publish(finding(summary="  ")) is None


def test_a_lookup_is_published_with_its_answer_and_source():
    (check,), _ = parse(
        [
            entry(
                kind="lookup",
                subject="extended support hours",
                claim="extended support hours",
                quote="extended support hours",
                segment=BY_ID["seg-06"],
            )
        ]
    )
    item = checks.publication(
        check,
        finding(
            verdict="answered",
            summary="Support from 7:00 to 22:00 Eastern on weekdays.",
            sources=["R1"],
        ),
        SOURCES,
        0.75,
    )
    assert item.type == "lookup" and item.title == "Lookup: extended support hours"
    assert item.body.startswith("Support from 7:00 to 22:00 Eastern on weekdays.")
    assert "Mentioned by Lee, 0:46." in item.body
    assert publish(finding(verdict="answered")) is None  # not a fact check's verdict


def test_a_finding_cannot_add_links_or_markup_of_its_own():
    item = publish(
        finding(
            summary="Prices rose. [Click here](https://attacker.example/x) or visit "
            "https://attacker.example/y now! ![p](https://attacker.example/i.png) <b>x</b> # Heading"
        )
    )
    assert "attacker.example" not in item.body
    assert "\\<b\\>" in item.body and "\\# Heading" in item.body
    assert item.body.count("](") == 1  # the source's own link only


def test_a_long_body_keeps_what_the_sources_say_and_never_cuts_a_link():
    (check,), _ = parse([entry()])
    long = {
        f"R{n}": {
            "title": f"Intranet page {n}",
            "url": f"https://intranet.example.com/sites/{n}/" + "a" * 1900,
            "via": "Documents",
        }
        for n in range(1, 4)
    }
    item = checks.publication(
        check,
        finding(sources=["R1", "R2", "R3"], summary="Prices rose to 46,000. " * 20),
        long,
        0.75,
    )
    assert len(item.body) <= checks.MAX_BODY_CHARS
    assert "**What the sources say:**" in item.body and "Confidence: high" in item.body
    # The long addresses are named by title, whole, rather than cut.
    assert "Intranet page 1 (Documents)" in item.body and "](<" not in item.body
    assert item.body.count("(Documents)") == 3


def test_a_body_keeps_its_links_when_they_fit():
    item = publish()
    assert "[Support price list 2026](<https://example.com/prices>) (Documents)" in item.body


def test_a_source_link_must_be_a_web_address():
    (check,), _ = parse([entry()])
    sources = {"R1": {"title": "x", "url": "javascript:alert(1)", "via": "Web"}}
    item = checks.publication(check, finding(sources=["R1"]), sources, 0.75)
    assert "](" not in item.body and "javascript" not in item.body


def test_plain_text_is_cut_and_escaped():
    assert checks.plain("a * b _c_", 100) == "a \\* b \\_c\\_"
    assert checks.plain("x" * 50, 10) == "x" * 9 + "…"
