#!/usr/bin/env bash
#
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
#
# Build the stereo fixture used by diarization-spike.ts and diarization-e2e.ts.
#
# Shape (this matters — a fixture with one voice per channel cannot answer the
# questions these scripts ask):
#
#   ch_0  "system / meeting audio"  Alice (Joanna) and Bob (Matthew) ALTERNATING
#                                   over six turns — two voices sharing a channel
#   ch_1  "microphone"              Bob (Matthew) only — deliberately the SAME
#                                   voice as one of ch_0's, which is how we tell
#                                   whether Transcribe numbers speakers per
#                                   channel or across the whole stream
#
# Each utterance names its own speaker and channel, so the transcript can be
# checked against the intended attribution by eye.
#
# Requires: aws cli (Amazon Polly access), python3. Costs a few Polly characters.
#
# Usage: ./test/make-diarization-fixture.sh [outdir]     # default /tmp/lma-diarization
set -euo pipefail

OUTDIR="${1:-/tmp/lma-diarization}"
REGION="${AWS_REGION:-us-west-2}"
RATE=16000
mkdir -p "$OUTDIR"

say() { # say <voice> <stem> <text>
  aws polly synthesize-speech --region "$REGION" \
    --output-format pcm --sample-rate "$RATE" \
    --voice-id "$1" --text "$3" "$OUTDIR/$2.pcm" >/dev/null
  printf '  %-4s %-8s %s\n' "$2" "$1" "$(du -h "$OUTDIR/$2.pcm" | cut -f1)"
}

echo "ch_0 — two voices alternating:"
say Joanna  a0 "This is Alice speaking on the meeting channel. I would like to open with the quarterly budget review."
say Matthew b0 "This is Bob speaking on the meeting channel. Thanks Alice. The migration timeline slipped by about two weeks."
say Joanna  a1 "Alice again on the meeting channel. Can we quantify the impact of that slip on the launch date?"
say Matthew b1 "Bob again on the meeting channel. Roughly one sprint, assuming the data backfill completes on schedule."
say Joanna  a2 "Alice one more time on the meeting channel. Let us take the staffing question to the follow up meeting."
say Matthew b2 "Bob one more time on the meeting channel. Agreed, I will send round a written summary this afternoon."

echo "ch_1 — one voice, same speaker as ch_0's Bob:"
say Matthew m0 "This is the microphone channel. Bob is talking into the conference room microphone now."
say Matthew m1 "Microphone channel again. Still Bob, the same voice that also appears on the meeting channel."
say Matthew m2 "Microphone channel one last time. Bob signing off from the conference room microphone."

OUTDIR="$OUTDIR" RATE="$RATE" python3 - <<'PY'
import array, os, wave

outdir = os.environ["OUTDIR"]
rate = int(os.environ["RATE"])
gap = int(0.8 * rate)
ch0_stems = ["a0", "b0", "a1", "b1", "a2", "b2"]
ch1_stems = ["m0", "m1", "m2"]
# Place each ch_1 utterance at the start of a ch_0 *Alice* turn, so the shared
# voice (Bob) never overlaps itself across the two channels.
ch1_at_turn = [0, 2, 4]


def load(stem):
    a = array.array("h")
    with open(os.path.join(outdir, stem + ".pcm"), "rb") as fh:
        a.frombytes(fh.read())
    return a


ch0 = array.array("h")
turn_starts = []
for stem in ch0_stems:
    turn_starts.append(len(ch0))
    ch0.extend(load(stem))
    ch0.extend(array.array("h", bytes(gap * 2)))

total = len(ch0)
ch1 = array.array("h", bytes(total * 2))
for stem, turn in zip(ch1_stems, ch1_at_turn):
    part = load(stem)
    start = turn_starts[turn]
    end = min(start + len(part), total)
    ch1[start:end] = part[: end - start]

interleaved = array.array("h", bytes(total * 2 * 2))
interleaved[0::2] = ch0
interleaved[1::2] = ch1

path = os.path.join(outdir, "diarization-stereo.wav")
with wave.open(path, "wb") as o:
    o.setnchannels(2)
    o.setsampwidth(2)
    o.setframerate(rate)
    o.writeframes(interleaved.tobytes())

print("\nwrote %s — %.1fs stereo PCM16 @ %d Hz" % (path, total / rate, rate))
for stem, start in zip(ch0_stems, turn_starts):
    who = "Alice/Joanna" if stem.startswith("a") else "Bob/Matthew"
    print("  ch_0 %s %-13s @ %6.2fs" % (stem, who, start / rate))
for stem, turn in zip(ch1_stems, ch1_at_turn):
    print("  ch_1 %s Bob/Matthew   @ %6.2fs" % (stem, turn_starts[turn] / rate))
PY
