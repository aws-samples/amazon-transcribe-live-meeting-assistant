# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Transcript Segment type field selector"""

from typing import Tuple
from gql.dsl import DSLField, DSLSchema


def transcript_segment_fields(schema: DSLSchema) -> Tuple[DSLField, ...]:
    """Transcript Segment type field selector"""
    return (
        schema.TranscriptSegment.PK,
        schema.TranscriptSegment.SK,
        schema.TranscriptSegment.CallId,
        schema.TranscriptSegment.UpdatedAt,
        schema.TranscriptSegment.CreatedAt,
        schema.TranscriptSegment.Channel,
        schema.TranscriptSegment.SegmentId,
        schema.TranscriptSegment.StartTime,
        schema.TranscriptSegment.EndTime,
        schema.TranscriptSegment.Speaker,
        schema.TranscriptSegment.Transcript,
        schema.TranscriptSegment.IsPartial,
        schema.TranscriptSegment.Owner,
        schema.TranscriptSegment.SharedWith,
    )
