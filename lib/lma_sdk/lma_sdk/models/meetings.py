# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Meeting data models: the meetings a selection finds, and the outcome of deleting them."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MeetingRef(BaseModel):
    """One meeting a selection found: what identifies it, and what it is listed by."""

    call_id: str
    owner: str = ""
    created_at: str = ""
    status: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    #: The keys of the meeting's list row, which the per-meeting delete path takes.
    list_pk: str = ""
    list_sk: str = ""

    @property
    def in_progress(self) -> bool:
        """The meeting is still being captured (STARTED or TRANSCRIBING)."""
        return self.status in ("STARTED", "TRANSCRIBING")


class MeetingFailure(BaseModel):
    """A meeting the delete path did not delete, and why."""

    call_id: str
    error: str


class MeetingDeleteResult(BaseModel):
    """What ``client.meetings.delete_by_labels`` found and did.

    ``dry_run`` is true unless the deletion was confirmed; a dry run deletes
    nothing and leaves ``deleted`` and ``failed`` empty.
    """

    dry_run: bool
    user: str
    #: ``all`` when the user is in the Admin group, else ``own``.
    scope: str
    labels: dict[str, str]
    #: The meetings that match and would be, or were attempted to be, deleted.
    matched: list[MeetingRef] = Field(default_factory=list)
    #: Matching meetings still in progress, which are never deleted.
    skipped_in_progress: list[MeetingRef] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    failed: list[MeetingFailure] = Field(default_factory=list)
