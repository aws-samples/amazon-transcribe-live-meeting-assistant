# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Hand a starting transcription session the voices it may label by name.

The WebSocket transcriber calls this once, when a meeting's ASR sessions open.
It answers with the enrolled voiceprints that consent currently permits, each
with the name to label it with.

Why this is a Lambda rather than the transcriber reading the table itself:

* **The consent rules stay in one implementation.** Whether an enrollment may be
  used turns on four promises made to the person who enrolled (``pure.
  is_usable_for_matching``), and a second copy of those rules in another
  language would drift. The transcriber receives a decision, not the inputs to
  one.
* **One role reads the biometrics.** Only the voice-enrollment roles hold read
  access to the enrollment table and its key; the transcriber's task role does
  not, and does not need to.

The response carries no subject ids. The transcriber needs a label and a vector
to recognise somebody; which LMA account that is belongs to the portal and the
audit log, and sending it would put it in a Fargate task's memory and logs for
no purpose.

All of the judgement lives in pure.py; this module is the AWS plumbing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import boto3
import pure

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENROLLMENT_TABLE = os.environ["VOICE_ENROLLMENT_TABLE"]
TERMS_VERSION = os.environ.get("VOICE_ENROLLMENT_TERMS_VERSION", "1")

# Matches MAX_ENROLLED_VOICES in the transcriber, which enforces the same bound
# on what it puts on the wire. Kept in step deliberately: if this sent more, the
# transcriber would silently drop the excess in an order this function did not
# choose, so the decision about which voices are cut is made here, where the
# enrollment dates are known.
MAX_SEEDS = int(os.environ.get("VOICE_ENROLLMENT_MAX_SEEDS", "32"))

# Same reasoning as the retention pass: a table that keeps handing back a
# continuation key must not spin this to its timeout. A meeting is waiting on
# this call, so the bound matters more here than there.
MAX_PAGES = 20

dynamodb = boto3.client("dynamodb")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_enrollments():
    """Every enrollment, paged.

    A Scan, for the same reason the retention pass uses one: the question is
    "which voiceprints may be used", which is a question about all of them, and
    the table holds one row per enrolled person rather than anything at
    meeting scale.
    """
    from index import _from_item  # noqa: PLC0415 - shared serialisation

    items = []
    start_key = None
    pages = 0
    while pages < MAX_PAGES:
        kwargs = {"TableName": ENROLLMENT_TABLE}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = dynamodb.scan(**kwargs)
        items.extend(_from_item(item) for item in response.get("Items", []))
        pages += 1
        next_key = response.get("LastEvaluatedKey")
        if not next_key or next_key == start_key:
            break
        start_key = next_key
    if pages >= MAX_PAGES:
        LOGGER.warning("Stopped reading enrollments at the %d page limit", MAX_PAGES)
    return items


def handler(event, context):  # pylint: disable=unused-argument
    """Entry point for the transcriber's invoke.

    Returns an empty list rather than an error for anything that goes wrong: a
    meeting that cannot be seeded still transcribes perfectly well, with voice
    ids a person or the transcript miner can name afterwards. Failing the call
    would risk the transcript itself, which is a far worse outcome than labels
    that say `spk_0`.
    """
    call_id = (event or {}).get("CallId") or ""
    try:
        enrollments = read_enrollments()
        voices, usable_total = pure.session_seeds(enrollments, TERMS_VERSION, _now(), MAX_SEEDS)
    except Exception:  # noqa: BLE001 - see the docstring
        LOGGER.exception("Could not read voice enrollments for %s", call_id)
        return {"EnrolledVoices": []}

    if usable_total > len(voices):
        LOGGER.warning(
            "More enrolled voices than one session is seeded with: sending %d of %d",
            len(voices),
            usable_total,
        )
    # Counts only. The point of this function is to move names and voiceprints,
    # and neither belongs in a log line.
    LOGGER.info(
        "Seeding %s with %d of %d enrollment(s)", call_id, len(voices), len(enrollments)
    )
    return {
        "EnrolledVoices": [
            {"label": voice["label"], "vector": voice["vector"]} for voice in voices
        ]
    }
