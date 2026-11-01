# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""VideoKeyframe type field selector"""

from typing import Tuple

from gql.dsl import DSLField, DSLSchema


def video_keyframe_fields(schema: DSLSchema) -> Tuple[DSLField, ...]:
    """VideoKeyframe type field selector.

    The onAddVideoKeyframe subscription delivers only what the mutation
    selected, so this is every field the meeting page reads, plus Owner and
    SharedWith, which the subscription filter needs.
    """
    return (
        schema.VideoKeyframe.PK,
        schema.VideoKeyframe.SK,
        schema.VideoKeyframe.CallId,
        schema.VideoKeyframe.FrameId,
        schema.VideoKeyframe.CreatedAt,
        schema.VideoKeyframe.Timestamp,
        schema.VideoKeyframe.VideoOffset,
        schema.VideoKeyframe.ImageUrl,
        schema.VideoKeyframe.ThumbnailUrl,
        schema.VideoKeyframe.Width,
        schema.VideoKeyframe.Height,
        schema.VideoKeyframe.Source,
        schema.VideoKeyframe.ShareDetected,
        schema.VideoKeyframe.ChangeScore,
        schema.VideoKeyframe.Owner,
        schema.VideoKeyframe.SharedWith,
    )
