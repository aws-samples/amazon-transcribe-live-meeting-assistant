# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's download archives (meeting_archive) are deleted with the meeting.

Same setup as test_keyframes.py: no AWS, the AppSync client and the GraphQL DSL
replaced before import.
"""

from __future__ import annotations

from test_delete_metering_records import index


def test_deleting_a_meetings_files_deletes_its_archive_folder(monkeypatch) -> None:
    prefixes = []
    monkeypatch.setattr(
        index, "_batch_delete_prefix", lambda bucket, prefix: prefixes.append(prefix) or 0
    )
    index.delete_recordings_transcripts("m 1")
    # The meeting's folder under the archive prefix, with a trailing slash so
    # that meeting "m 1" does not take "m 10"'s archives with it.
    assert "lma-meeting-archives/m_1/" in prefixes
    assert len(prefixes) == 3
