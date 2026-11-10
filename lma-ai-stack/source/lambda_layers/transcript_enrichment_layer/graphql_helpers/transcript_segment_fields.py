# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Transcript Segment type field selector"""

from typing import Tuple

from gql.dsl import DSLField, DSLSchema


def _language_code(schema: DSLSchema) -> Tuple[DSLField, ...]:
    """TranscriptSegment.LanguageCode, when the schema the API is running declares it.

    Selected so that onAddTranscriptSegment, which AppSync fills from this
    mutation's selection, carries the segment's language to the browser.
    Conditional for the reason call_fields._summary_state is: during a stack
    update the running schema can be older than this layer.
    """
    # pylint: disable-next=protected-access
    if "LanguageCode" in schema.TranscriptSegment._type.fields:
        return (schema.TranscriptSegment.LanguageCode,)
    return ()


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
        *_language_code(schema),
        schema.TranscriptSegment.Owner,
        schema.TranscriptSegment.SharedWith,
    )
