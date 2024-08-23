# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
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
    )
