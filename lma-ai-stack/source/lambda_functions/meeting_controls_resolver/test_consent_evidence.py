# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""A meeting's consent evidence images are deleted with the meeting.

Same setup as test_keyframes.py: no AWS, the AppSync client and the GraphQL DSL
replaced before import.
"""

from __future__ import annotations

from test_delete_metering_records import index


def test_deleting_a_meeting_deletes_its_evidence_folder(monkeypatch) -> None:
    prefixes = []
    monkeypatch.setattr(
        index, "_batch_delete_prefix", lambda bucket, prefix: prefixes.append(prefix) or 2
    )
    assert index.delete_consent_evidence("m 1") == 2
    # The folder the consent resolver accepts evidence from, with a trailing
    # slash so that meeting "m 1" does not take "m 10"'s images with it.
    assert prefixes == ["lma-consent-evidence/m_1/"]


def test_a_meeting_without_evidence_deletes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(index, "_batch_delete_prefix", lambda bucket, prefix: 0)
    assert index.delete_consent_evidence("m-2") == 0
