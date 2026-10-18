# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Deciding which transcript voice belongs to which person, without AWS.

Speaker separation can tell voices apart but has no way to know who they are, so
a meeting captured from a conference-room microphone reads `Other Participant
(spk_0)`. People do say who they are, though -- usually in the first minute, to
each other rather than to any assistant -- and they address each other by name
throughout. This module turns that into a label-to-name map.

The design principle is that **the model proposes and this module decides**. A
language model asked to attribute names to speaker labels will occasionally
return a label that does not exist, or attach a name to the wrong one. So the
model is asked only for a name, the kind of evidence, and the TURN NUMBER that
evidence is in; the attribution is then recomputed here from the turn index,
which is authoritative. A mislabelled proposal is dropped rather than applied.

The two kinds of evidence are treated very differently, because they are not
equally reliable:

  * **Self-introduction** -- "Hi everyone, I'm Sarah Lee" -- names the speaker of
    that turn. Near-unambiguous, so it is applied as `confirmed`.
  * **Addressed by name** -- "That's a good point, Sarah" -- names the speaker of
    the PREVIOUS turn, and only when that turn is a different voice. It inherits
    every separation error: in live testing, the first seconds of one speaker's
    turn were absorbed into the previous speaker's run, which is exactly the
    situation that makes this inference attribute a name to the wrong person. So
    it is only ever recorded as `proposed`, for a human to confirm.

Everything here is pure so the policy can be tested without Bedrock or DynamoDB.
"""

from __future__ import annotations

import json
import re

# Shared with the UI's speaker-identities.js -- see the note there on why the key
# is the channel plus the speaker string AS STORED.
DEFAULT_OTHER_SPEAKER_NAME = "Other Participant"
# What the client calls the local microphone channel before anybody is named.
# Just as much a placeholder as the one above, and leaving it out meant a voice
# on the microphone channel could never be named at all -- the same
# over-rejection as every other guard in this module's history.
DEFAULT_LOCAL_SPEAKER_NAME = "Me"
PLACEHOLDER_SPEAKERS = (DEFAULT_OTHER_SPEAKER_NAME, DEFAULT_LOCAL_SPEAKER_NAME, "")
DIARIZATION_LABEL = re.compile(r"^(.*?)(\s*\(spk_\d+\))$")

# Channels that carry a nameable human voice.
NAMEABLE_CHANNELS = ("CALLER", "AGENT")

SELF_INTRODUCTION = "self-introduction"
ADDRESSED_BY_NAME = "addressed-by-name"

# A name the model returned that is not a name. Models reach for these when the
# transcript does not actually support an attribution, and an unnamed voice is
# far better than a voice called "Speaker 1".
# A string with no letters in it at all -- digits, spaces and punctuation only.
# Checked FIRST, and separately, which is what keeps digit runs out of the
# alternation below: a repeated group containing `\d+` next to a separator that
# could match empty backtracks exponentially, and a 28-character value measured
# at 96 seconds, enough on its own to exhaust this function's timeout. `\W` is
# Unicode-aware here, so a name in any script survives this test.
NO_LETTERS = re.compile(r"^[\W\d_]+$", re.UNICODE)

# Words that are a role or a placeholder rather than somebody's name.
GENERIC_WORD = (
    r"unknown|unidentified|unnamed|speaker|participant|attendee|guest|caller|agent|host|"
    r"person|male|female|man|woman|voice|other|the|none|null|spk|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|\d+"
)
# A "name" made up entirely of generic words is not a name, in any combination:
# "unknown", "Speaker one", "unidentified male", "the other person".
#
# The separator is `[\s_-]+`, requiring at least one character, and the group
# repeats a separator-plus-token pair rather than a token alone. That is what
# makes `\d+` safe to include here: every position is unambiguous, because a run
# of digits with no separator in it can only be one token. The earlier form,
# `((WORD)[\s_-]*)+` with an optional separator, let the engine split the same run
# in exponentially many ways -- a 28-character value took 96 seconds.
NON_NAMES = re.compile(rf"^({GENERIC_WORD})([\s_-]+({GENERIC_WORD}))*$", re.IGNORECASE | re.UNICODE)

# Longest name worth storing. Beyond this the model has returned a sentence.
MAX_NAME_LENGTH = 60

# Exact placeholders, matched whole and case-insensitively. Kept as a literal set
# rather than regex alternatives because they contain punctuation that the
# token separator below deliberately does not allow.
# Compared with dots and slashes removed, so "N/A", "n.a." and "na" are one
# entry rather than three that each have to be remembered -- and so the trailing
# punctuation stripped off a name earlier cannot make an entry unreachable.
PLACEHOLDER_NAMES = frozenset(
    {"na", "tbd", "tba", "anonymous", "anon", "redacted", "withheld", "unassigned", "?"}
)


def _placeholder_key(name):
    return re.sub(r"[./\s]+", "", name.casefold())


# Phrases worth sending to a model at all. The miner runs repeatedly during a
# meeting, and most passes see nothing that could name anybody; this keeps those
# from costing a model call. Deliberately generous -- a missed pass costs a
# naming opportunity, and the end-of-meeting pass sees the whole transcript
# anyway.
#
# This list is Latin-script, and mostly English. That is a limit on what it can
# be used for, NOT a limit on which meetings get named: see
# `shortcut_is_meaningful_for`. Treating a miss as "nothing here" meant a Russian,
# Chinese or Thai meeting never reached the model at all -- the feature was
# silently off for those languages, which is the same mistake four other guards
# in this module made before it.
NAMING_HINT = re.compile(
    r"\b("
    # English
    r"i'?m|i\s+am|my\s+name|this\s+is|here\b|call\s+me|speaking\b|"
    r"thanks|thank\s+you|good\s+point|agree|right|exactly|over\s+to|"
    r"what\s+do\s+you|you\s+want\s+to|go\s+ahead|"
    # The other widely-spoken Latin-script languages, so a French or Spanish
    # meeting is not paying for a pass it did not need either.
    r"je\s+m'appelle|je\s+suis|moi\s+c'est|"  # French
    r"ich\s+bin|mein\s+name|"  # German
    r"me\s+llamo|yo\s+soy|soy\s+|"  # Spanish
    r"meu\s+nome|eu\s+sou|"  # Portuguese
    r"mi\s+chiamo|sono\s+|"  # Italian
    r"nazywam\s+si|nazywam|mam\s+na\s+imi|"  # Polish
    r"benim\s+ad|ad\u0131m|ben\s+|"  # Turkish
    r"t\u00f4i\s+t\u00ean|t\u00ean\s+t\u00f4i|t\u00f4i\s+l\u00e0|"  # Vietnamese
    r"nama\s+saya|saya\s+|"  # Indonesian / Malay
    r"mijn\s+naam|ik\s+ben|"  # Dutch
    r"jag\s+heter|mitt\s+namn|"  # Swedish
    r"jeg\s+heter|jeg\s+er|"  # Norwegian / Danish
    r"nimeni\s+on|olen\s+"  # Finnish
    r")\b",
    re.IGNORECASE,
)

# Any letter, in any script -- `[^\W\d_]` is the standard idiom for that. Named
# for what it matches, because an earlier name said the opposite.
ANY_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
# Latin and its extensions, including Latin Extended Additional (U+1e00-U+1eff),
# without which Vietnamese was counted as non-Latin.
LATIN_LETTER = re.compile(r"[A-Za-zÀ-ɏḀ-ỿ]")

# Where one spoken word ends and the next begins: whitespace and punctuation,
# and nothing else.
#
# Deliberately NOT `[^\w']+`. Python's `\w` excludes Unicode combining marks,
# which are how most Indic scripts, and Arabic and Hebrew vocalisation, are
# written -- so splitting on its complement shatters a word at every vowel sign:
# `नरेन्द्र` came out as `नर`, `न`, `द`, `र`, and no Hindi name could ever match
# the turn that contained it. Splitting on explicit separators keeps marks
# attached to the letter they belong to, in every script.
#
# The apostrophe is excluded from the separators on purpose, so O'Brien stays one
# word; `!-&` and `(-/` are the ASCII punctuation either side of it.
WORD_SEPARATOR = re.compile(
    "[\\s"
    "\\u0021-\\u0026\\u0028-\\u002f\\u003a-\\u0040"  # ASCII punctuation, minus '
    "\\u005b-\\u0060\\u007b-\\u007e\\u00a1-\\u00bf"
    # General punctuation, in TWO pieces. The gap between them is the point:
    # U+200b-U+200f and U+202a-U+202e are format characters, not punctuation,
    # and U+200c/U+200d (ZERO WIDTH NON-JOINER and JOINER) are how Persian,
    # Urdu and several Indic scripts control conjunct and medial forms INSIDE
    # a word. Treating them as boundaries shattered a compound given name,
    # which is the same mistake as splitting Devanagari at its vowel signs in
    # a different script. U+2060-U+206f are format characters too.
    "\\u2010-\\u2027\\u2030-\\u205e"
    "\\u3000-\\u303f"  # CJK punctuation
    "\\u0589\\u060c\\u061b\\u061f"  # Armenian, Arabic
    "\\u0964\\u0965"  # Devanagari danda
    "\\u3001\\u3002\\uff0c\\uff0e\\uff1a\\uff1b\\uff1f\\uff01"  # fullwidth
    "]+"
)

# Apostrophes a transcript or a model may use interchangeably. Normalised to
# the ASCII one on both sides before comparing, because the ASCII apostrophe
# is excluded from the separators above and the typographic ones are not -- so
# without this, "O’Brien" failed against a turn that said exactly that.
# Language models emit U+2019 routinely.
APOSTROPHES = str.maketrans({"\u2019": "'", "\u02bc": "'", "\u2018": "'", "\u00b4": "'"})


def shortcut_is_meaningful_for(text):
    """Could the phrase list have matched this text at all?

    The shortcut exists to avoid paying for a model call on a transcript that
    plainly names nobody. It can only make that judgement about a language it
    can read, so for anything substantially outside the Latin script it declines
    to judge and the pass runs. Cost is still bounded -- by the cooldown and by
    requiring the transcript to have grown -- and those two are language-neutral.
    """
    letters = ANY_LETTER.findall(text or "")
    if not letters:
        return True
    latin = sum(1 for character in letters if LATIN_LETTER.match(character))
    return latin * 2 >= len(letters)


def parse_identities(raw):
    """Read the stored map, tolerating anything.

    Same contract as parseSpeakerIdentities in the UI and the twin in
    FetchTranscript: a meeting that predates the feature has no attribute, and a
    blob written by a newer version may carry keys this does not know. Both must
    yield a usable map rather than an error -- a pass that cannot read the
    existing names would re-propose names a human has already settled.
    """
    empty = {
        "version": 1,
        "candidates": [],
        "identities": {},
        "merges": {},
        "segmentOverrides": {},
        # Names a user has rejected for a voice. Honoured so a pass cannot keep
        # proposing something a person has already said is wrong.
        "rejected": {},
    }
    if not raw:
        return empty
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return empty
    if not isinstance(parsed, dict):
        return empty
    out = dict(empty)
    out["version"] = parsed.get("version", 1)
    candidates = parsed.get("candidates")
    out["candidates"] = (
        [c for c in candidates if isinstance(c, str)] if isinstance(candidates, list) else []
    )
    for section in ("identities", "merges", "segmentOverrides", "rejected"):
        value = parsed.get(section)
        out[section] = value if isinstance(value, dict) else {}
    last_pass = parsed.get("lastPass")
    if isinstance(last_pass, dict):
        out["lastPass"] = last_pass
    return out


def speaker_key(channel, speaker):
    """Key for one voice: its channel plus the speaker string as stored."""
    return f"{channel or ''}#{(speaker or '').strip()}"


def split_diarization_label(speaker):
    """Split a stored speaker into its base name and its engine voice id."""
    match = DIARIZATION_LABEL.match(speaker or "")
    return (match.group(1), match.group(2)) if match else (speaker or "", "")


def build_turns(segments):
    """Order the final segments into numbered conversational turns.

    Turn numbers are what the model cites as evidence and what attribution is
    recomputed from, so they must be stable for one pass: the list is sorted by
    start time, then end time, then segment id, so segments that share a
    timestamp do not reorder between the prompt and the verification.

    Partial segments are excluded: a partial carries no voice id yet (labels
    arrive only on final results), so including one would invite the model to
    name a label that is about to change.
    """
    rows = []
    for segment in segments or []:
        if segment.get("IsPartial") is True:
            continue
        if segment.get("Channel") not in NAMEABLE_CHANNELS:
            continue
        text = (segment.get("Transcript") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "channel": segment.get("Channel"),
                "speaker": segment.get("Speaker") or "",
                "key": speaker_key(segment.get("Channel"), segment.get("Speaker")),
                "text": text,
                "segment_id": segment.get("SegmentId"),
                "start": float(segment.get("StartTime") or 0),
                "end": float(segment.get("EndTime") or 0),
            }
        )
    rows.sort(key=lambda r: (r["start"], r["end"], str(r["segment_id"])))
    for number, row in enumerate(rows, start=1):
        row["turn"] = number
    return rows


def select_turns(turns, max_turns):
    """Trim a long transcript to the turns most likely to name somebody.

    Naming evidence clusters at the start, where people introduce themselves, so
    a simple head slice is nearly right -- but only nearly. Taking the head alone
    means that past the limit every pass sees byte-identical input, so the
    end-of-meeting pass is no better than the first one and a late introduction
    ("by the way, I'm Priya") can never be found. So keep the head and the most
    recent turns, which both covers that and keeps later passes able to learn
    something.

    Turn numbers are preserved, not renumbered: they are what attribution is
    recomputed from, and the prompt shows the same numbers. A gap in the middle
    is visible to the model and harmless -- it cites a number that exists.
    """
    if max_turns <= 0 or len(turns) <= max_turns:
        return list(turns)
    head = max(1, (max_turns * 2) // 3)
    tail = max_turns - head
    if tail <= 0:
        # turns[-0:] is the WHOLE list, so a limit this small would return more
        # than it was asked for, with a duplicated first turn.
        return list(turns[:head])
    return list(turns[:head]) + list(turns[-tail:])


def voices_in(turns):
    """Every distinct voice key in the transcript, in first-heard order."""
    seen = {}
    for turn in turns:
        seen.setdefault(turn["key"], turn)
    return list(seen.keys())


def settled_keys(identities):
    """Voices a human has settled, which a model pass must never overwrite.

    A name somebody typed, or locked, outranks anything inferred -- the whole
    point of the proposed/confirmed split is that human judgement wins.
    """
    settled = set()
    for key, entry in (identities.get("identities") or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("locked") is True or entry.get("source") == "manual":
            settled.add(key)
    return settled


def unresolved_voices(turns, identities):
    """Voices with no name yet that a pass could usefully work on.

    A voice whose stored base name already reads as a person -- a meeting roster
    name from a Virtual Participant, or a channel name the client supplied -- is
    not unresolved, even though it carries a voice id.
    """
    # A *proposed* name is not resolved: nobody has confirmed it, and the
    # person may yet introduce themselves, which would upgrade it. Only a
    # confirmed name takes a voice out of scope. An entry with no status at all
    # is treated as confirmed, since that is what a human rename writes.
    named = {
        key
        for key, entry in (identities.get("identities") or {}).items()
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
        and entry.get("status") != "proposed"
    }
    merges = identities.get("merges") or {}
    out = []
    for key in voices_in(turns):
        if merges.get(key):
            # Folded into another voice; the target is what carries the name, and
            # it is reached through its own segments.
            continue
        if key in named or key in settled_keys(identities):
            continue
        base, suffix = split_diarization_label(key.split("#", 1)[-1])
        if base not in PLACEHOLDER_SPEAKERS:
            # Already reads as a person: a meeting-roster name from a Virtual
            # Participant, or a channel name the client supplied.
            continue
        if not suffix and base in PLACEHOLDER_SPEAKERS:
            # A whole channel with no separation: nameable, and worth asking about.
            out.append(key)
            continue
        out.append(key)
    return out


def within_cooldown(identities, now_iso, cooldown_seconds):
    """Has a pass run too recently to be worth paying for another?

    Enforced here rather than in the browser, because the browser is where the
    timer lives and a client cannot be trusted to pace a paid model call. Each
    pass stamps the map, and a pass that reads the same transcript again a few
    seconds later cannot learn anything new.

    A pass is also pointless if the transcript has not grown, so the stamp
    records the turn count too: a cooldown that has elapsed still does not earn a
    model call if nobody has said anything since.
    """
    stamp = (identities.get("lastPass") or {}) if isinstance(identities, dict) else {}
    ran_at = stamp.get("at")
    if not ran_at:
        return False
    try:
        elapsed = (_parse_iso(now_iso) - _parse_iso(ran_at)).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed < float(cooldown_seconds)


def transcript_has_grown(identities, total_turns):
    """Has anybody spoken since the last pass?

    Takes the RAW number of turns in the transcript, never the trimmed list the
    model sees. Those two are the same only on short meetings: past the trim
    limit the trimmed length saturates, so comparing it against a stamp taken
    from it made this permanently false and silenced naming for the rest of the
    meeting -- the end-of-meeting pass and the button included.
    """
    stamp = (identities.get("lastPass") or {}) if isinstance(identities, dict) else {}
    previous = stamp.get("turns")
    if not isinstance(previous, int):
        return True
    return total_turns > previous


def stamp_pass(identities, now_iso, turn_count):
    """Record that a pass ran, so the next one can decline cheaply."""
    stamped = dict(identities)
    stamped["lastPass"] = {"at": now_iso, "turns": int(turn_count)}
    return stamped


def _parse_iso(value):
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def worth_a_model_call(turns, identities, force=False):
    """Is there anything in this transcript that could name an unnamed voice?

    Skips the model when no turn contains so much as an introduction or a thanks.
    `force` is the on-demand path: somebody pressed the button, so answer them
    even if the heuristic says there is nothing there.

    The phrase list is Latin-script, so it can only answer this question about a
    transcript it could have read. For a turn substantially outside that script
    the shortcut declines to judge and the pass runs -- otherwise naming is
    silently unavailable for whole languages, which is what happened before.
    """
    if not unresolved_voices(turns, identities):
        return False
    if force:
        return True
    for turn in turns:
        if not shortcut_is_meaningful_for(turn["text"]):
            return True
        if NAMING_HINT.search(turn["text"]):
            return True
    return False


def format_transcript(turns):
    """The numbered transcript the model is asked to read."""
    return "\n".join(f"[{t['turn']}] {t['key']}: {t['text']}" for t in turns)


PROMPT = """You are reading a meeting transcript in which the speech recognition \
engine could separate the voices but could not know who they are. Each line is one \
turn, numbered, and prefixed with the engine's label for that voice.

Your task is to find, in the words people actually say, evidence of which real \
person each label belongs to.

There are exactly two kinds of usable evidence:

1. "{self_intro}" -- the speaker says who they are. For example "Hi everyone, I'm \
Sarah Lee" or "Bob Strahan here" or "my name is Priya". The name belongs to the \
speaker of that turn.

2. "{addressed}" -- a speaker addresses, thanks or credits the person who spoke \
immediately before them, by name. For example "That's a good point, Sarah" or \
"Thanks Bob" or "Priya, do you want to take this?". The name belongs to the \
speaker of the PREVIOUS turn, not to the speaker saying it.

Everything else is NOT evidence. In particular:
- Someone mentioned who is not in the meeting ("Bob said yesterday that...") \
names nobody.
- A name used in the third person about another participant names nobody unless \
it is clearly addressed to the person who just spoke.
- Do not guess from role, topic, accent or speaking order.

If you cannot find evidence for a label, leave it out. Returning fewer labels is \
always better than guessing: a wrong name is worse than no name.
{candidates}
Labels still needing a name:
{labels}

Transcript:
{transcript}

Respond with a JSON array and nothing else. One object per label you can support:

[
  {{"label": "<the label exactly as it appears above>",
    "name": "<the person's name as spoken>",
    "basis": "{self_intro}" or "{addressed}",
    "turn": <the number of the turn containing the evidence>,
    "quote": "<the few words from that turn that are the evidence>"}}
]

If you can support none of them, respond with [].
"""

CANDIDATES_BLOCK = """
These names are expected to be in this meeting. Prefer them when the transcript \
supports one, matching loosely on spelling since the transcript is speech. Do not \
feel limited to this list -- somebody not on it may still introduce themselves:
{names}
"""


def build_prompt(turns, labels, candidates):
    """Assemble the prompt, including the optional pasted attendee list."""
    candidates_block = ""
    if candidates:
        candidates_block = CANDIDATES_BLOCK.format(
            names="\n".join(f"- {name}" for name in candidates)
        )
    return PROMPT.format(
        self_intro=SELF_INTRODUCTION,
        addressed=ADDRESSED_BY_NAME,
        candidates=candidates_block,
        labels="\n".join(f"- {label}" for label in labels),
        transcript=format_transcript(turns),
    )


def parse_model_response(raw):
    """Pull the proposal array out of whatever the model returned.

    Models wrap JSON in prose or fences often enough that insisting on a bare
    array would throw away good answers, so the first balanced array in the
    response is taken. Anything unparseable yields no proposals, which leaves the
    voices unnamed -- the safe direction.
    """
    if not raw:
        return []
    text = raw.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def clean_name(value):
    """A usable person name, or None."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split()).strip(" .,:;-\"'")
    if not name or len(name) > MAX_NAME_LENGTH:
        return None
    # Cheapest and most restrictive tests first, so nothing expensive ever sees a
    # pathological value.
    if NO_LETTERS.match(name):
        return None
    # A letter in ANY script. Testing for [A-Za-z] made every name in a non-Latin
    # script unnameable, which silently switched this feature off for Chinese,
    # Cyrillic, Arabic, Greek and Indic meetings.
    if not any(character.isalpha() for character in name):
        return None
    if len(name.split()) > 4:
        # A name is words, not a sentence.
        return None
    if _placeholder_key(name) in PLACEHOLDER_NAMES:
        return None
    if NON_NAMES.match(name):
        return None
    return name


def name_appears_in(name, text):
    """Is this name actually in those words?

    The last gap in the verification: the model picks the turn and the turn picks
    the voice, so a plausible-looking citation for a name nobody said would still
    be applied. Matching the name against the cited turn closes it.

    Matched loosely and per word, because the transcript is speech: "Sarah Lee"
    is supported by a turn that says "Sara Lee", and a first name alone is enough.
    """
    # Apostrophe variants normalised on both sides before anything else, so the
    # one that is excluded from the separators and the ones that are not cannot
    # disagree about where a word ends.
    folded_text = (text or "").casefold().translate(APOSTROPHES)
    folded_name = (name or "").casefold().translate(APOSTROPHES)

    # Chinese, Japanese, Thai, Khmer and Lao are written without spaces between
    # words, so tokenising makes a whole turn one token and no name can ever
    # match -- which switched naming off entirely for those languages.
    # Containment IS the word test there, and the over-matching that makes
    # substrings unsafe for Latin names ("An" inside "and") does not arise,
    # because a name in these scripts is not a fragment of unrelated words the
    # way a two-letter Latin name is.
    if UNSEGMENTED_SCRIPT.search(folded_name):
        return len(folded_name) >= 2 and folded_name in folded_text

    # Whole words on both sides for everything else. Substring matching was far
    # too generous: "An" counted the word "and" as support, "Mark" counted
    # "market", "Al" counted "also" -- so a short name was supported by almost
    # any turn.
    #
    # Apostrophes are not split out of the name, and are also compared with them
    # removed: splitting the name but not the text made "O'Brien" fail against a
    # turn that says "O'Brien".
    spoken = {word for word in WORD_SEPARATOR.split(folded_text) if word}
    spoken |= {word.replace("'", "") for word in spoken}
    parts = [part for part in re.split(r"[\s\-]+", folded_name) if len(part) > 1]
    if not parts:
        return False
    for part in parts:
        if part in spoken or part.replace("'", "") in spoken:
            return True
        # One transcription slip at the end of a longer word -- which is what
        # "Sarah" versus "Sara" is. Only for names long enough that the stem is
        # still distinctive.
        if len(part) >= 5 and any(word == part[:-1] or part == word[:-1] for word in spoken):
            return True
    return False


# Scripts written without spaces between words, where tokenising a turn yields
# one token and containment is the only workable test.
UNSEGMENTED_SCRIPT = re.compile(
    "["
    "぀-ヿ"  # Hiragana, Katakana
    "㐀-䶿一-鿿豈-﫿"  # CJK ideographs
    "฀-๿"  # Thai
    "຀-໿"  # Lao
    "ក-៿"  # Khmer
    "]"
)

# What a self-introduction sounds like IN ENGLISH. A turn that matches is taken
# as a real introduction and its name is applied; a turn that does not still
# yields a name, but only as a suggestion. It is deliberately not a veto: the
# list is English, and refusing on it named nobody at all for "Меня зовут Иван",
# "我是李明" or "Je m'appelle Sarah".
SELF_INTRODUCTION_PHRASE = re.compile(
    r"\b("
    r"i'?m|i\s+am|my\s+name|name'?s|this\s+is|it'?s\s+me|call\s+me|"
    r"speaking\b|here\b|joining|i'?ll\s+go|let\s+me\s+introduce"
    r")\b",
    re.IGNORECASE,
)


def looks_like_a_self_introduction(text):
    """Does this turn contain somebody naming themselves?"""
    return bool(SELF_INTRODUCTION_PHRASE.search(text or ""))


def attribute(proposal, turns_by_number):
    """Which voice a proposal actually names, recomputed from the turn index.

    This is the heart of the safety model. The model's own `label` is treated as
    a hint and checked, never trusted: for a self-introduction the name belongs
    to the speaker of the cited turn, and for an addressed-by-name it belongs to
    the speaker of the turn before it.

    Returns `(voice_key, evidence_turn)`, or `(None, None)` to drop the proposal.
    Both come from the same single parse of the cited turn number, so no caller
    has to re-look-up a turn that might not exist.
    """
    basis = proposal.get("basis")
    raw_turn = proposal.get("turn")
    # Must be a whole number. int() would truncate 1.5 to a real turn, quietly
    # attributing a name on the strength of a malformed answer.
    if isinstance(raw_turn, bool) or not isinstance(raw_turn, (int, str)):
        return None, None
    try:
        turn_number = int(str(raw_turn).strip())
    except (TypeError, ValueError):
        return None, None
    turn = turns_by_number.get(turn_number)
    if turn is None:
        return None, None

    if basis == SELF_INTRODUCTION:
        # The speaker named themselves, so the evidence turn IS the voice -- but
        # the model chose the turn, so without a label to cross-check against,
        # the model alone would be choosing the voice. Require it.
        if proposal.get("label") != turn["key"]:
            return None, None
        return turn["key"], turn

    if basis == ADDRESSED_BY_NAME:
        previous = turns_by_number.get(turn_number - 1)
        if previous is None:
            return None, None
        if previous["key"] == turn["key"]:
            # Somebody saying "good point, Sarah" in the middle of their own run
            # tells us nothing about who they were talking to.
            return None, None
        if proposal.get("label") != previous["key"]:
            return None, None
        return previous["key"], turn

    return None, None


def resolve_merge_target(key, merges):
    """Follow a merge chain to the voice that actually holds the name.

    Mirrors resolveMergeTarget in the UI, including stopping on a cycle. Needed
    here because the usual reason to merge two voices is that the engine split
    one person -- so an introduction very often sits on the half that was merged
    away, and dropping it would throw away the best evidence in the meeting.
    """
    current = key
    seen = {current}
    while merges.get(current) and merges[current] not in seen:
        current = merges[current]
        seen.add(current)
    return current


def merge_applied_onto(latest, applied_entries):
    """Fold just this pass's new names onto a freshly read map.

    A pass reads the stored map, spends several seconds on a model call, then
    writes. Writing the whole blob from the stale snapshot silently discarded any
    rename a user typed during that window -- and the write is deliberately
    last-write-wins, so nothing would report it. Only the keys this pass actually
    changed are applied, onto whatever is stored now, and a name that arrived in
    the meantime wins because it is settled and this is not.
    """
    merged = {
        "version": latest.get("version", 1),
        "candidates": list(latest.get("candidates") or []),
        "identities": dict(latest.get("identities") or {}),
        "merges": dict(latest.get("merges") or {}),
        "segmentOverrides": dict(latest.get("segmentOverrides") or {}),
        "rejected": dict(latest.get("rejected") or {}),
    }
    if isinstance(latest.get("lastPass"), dict):
        merged["lastPass"] = dict(latest["lastPass"])
    protected = settled_keys(latest)
    changed = False
    for key, entry in (applied_entries or {}).items():
        if key in protected:
            # Somebody named this voice while the pass was running.
            continue
        existing = merged["identities"].get(key)
        if (
            isinstance(existing, dict)
            and existing.get("status") == "confirmed"
            and entry.get("status") != "confirmed"
        ):
            continue
        rejected_here = merged["rejected"].get(key) or []
        name = entry.get("name")
        if any(str(name).casefold() == str(r).casefold() for r in rejected_here):
            continue
        merged["identities"][key] = entry
        changed = True
    return merged, changed


def apply_proposals(identities, proposals, turns, now_iso):
    """Fold verified proposals into the stored map.

    Returns `(updated_map, applied)` where `applied` describes what changed, for
    the log. The input map is not modified.

    The policy, in order of precedence:
      * a voice a human named or locked is never touched;
      * a self-introduction is applied as `confirmed`, because it is the speaker
        saying their own name;
      * an addressed-by-name is recorded as `proposed`, for a human to confirm,
        and never overwrites a `confirmed` one;
      * one name cannot be attached to two voices on the same channel, since that
        reads as one person in two places.
    """
    updated = {
        "version": identities.get("version", 1),
        "candidates": list(identities.get("candidates") or []),
        "identities": dict(identities.get("identities") or {}),
        "merges": dict(identities.get("merges") or {}),
        "segmentOverrides": dict(identities.get("segmentOverrides") or {}),
        "rejected": dict(identities.get("rejected") or {}),
    }
    if isinstance(identities.get("lastPass"), dict):
        updated["lastPass"] = dict(identities["lastPass"])
    turns_by_number = {t["turn"]: t for t in turns}
    nameable = set(unresolved_voices(turns, identities))
    protected = settled_keys(identities)
    applied = []

    def name_taken_on_channel(channel, name, except_key):
        for key, entry in updated["identities"].items():
            if key == except_key or not isinstance(entry, dict):
                continue
            if key.split("#", 1)[0] != channel:
                continue
            stored = entry.get("name")
            # A stored map is arbitrary JSON a client wrote, so an entry whose
            # name is not a string must not take naming down for this meeting.
            if isinstance(stored, str) and stored.casefold() == name.casefold():
                return True
        return False

    # Self-introductions first, so a confirmed name wins the channel-uniqueness
    # check against a weaker proposal for the same person.
    ordered = sorted(proposals, key=lambda p: p.get("basis") != SELF_INTRODUCTION)

    for proposal in ordered:
        name = clean_name(proposal.get("name"))
        if not name:
            continue
        basis = proposal.get("basis")
        if basis not in (SELF_INTRODUCTION, ADDRESSED_BY_NAME):
            continue
        key, evidence_turn = attribute(proposal, turns_by_number)
        if key is None:
            continue
        # An introduction on a voice that has been merged into another names the
        # target, not the merged-away half.
        key = resolve_merge_target(key, updated["merges"])
        if key not in nameable or key in protected:
            continue
        if not name_appears_in(name, evidence_turn["text"]):
            # The cited turn does not contain this name, so nothing in the
            # transcript supports it.
            continue
        # The model said somebody named themselves. Where the turn visibly reads
        # like an introduction the name is applied; where it does not, the name
        # is still offered, but only as a SUGGESTION for a human to confirm.
        #
        # A downgrade rather than a refusal, and the difference matters: the
        # phrase list is English, so refusing on it named nobody at all for
        # "Меня зовут Иван", "我是李明" or "Je m'appelle Sarah". Downgrading keeps
        # the safety property -- nothing dubious is applied as settled -- without
        # switching the feature off for everybody not speaking English, and it
        # also covers the English forms the list misses, like "Sarah Lee from
        # platform".
        introduces = basis != SELF_INTRODUCTION or looks_like_a_self_introduction(
            evidence_turn["text"]
        )
        existing = updated["identities"].get(key)
        if isinstance(existing, dict) and existing.get("status") == "confirmed":
            if basis != SELF_INTRODUCTION:
                continue
        # A name a user has already rejected for this voice is not proposed
        # again, however convincing the transcript looks to the model.
        rejected_here = updated["rejected"].get(key) or []
        if any(name.casefold() == str(r).casefold() for r in rejected_here):
            continue
        channel = key.split("#", 1)[0]
        if name_taken_on_channel(channel, name, key):
            continue

        status = "confirmed" if basis == SELF_INTRODUCTION and introduces else "proposed"
        # Only the model's quote if it is really in that turn. It is shown as the
        # reason to accept a name, so a sentence nobody said would undermine the
        # one decision the human is being asked to make.
        quote = proposal.get("quote")
        if not isinstance(quote, str) or quote.casefold() not in evidence_turn["text"].casefold():
            quote = evidence_turn["text"]
        updated["identities"][key] = {
            "name": name,
            "status": status,
            "source": basis,
            "updatedAt": now_iso,
            "evidence": {
                "quote": quote[:200],
                "segmentId": evidence_turn["segment_id"],
                "turn": evidence_turn["turn"],
            },
        }
        applied.append({"key": key, "name": name, "status": status, "basis": basis})

    return updated, applied
