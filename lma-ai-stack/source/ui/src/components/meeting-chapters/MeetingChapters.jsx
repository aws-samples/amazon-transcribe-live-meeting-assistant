/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's chapters, from the CHAPTERS section of its summary: a list of
 * times and titles. Selecting one moves the transcript to that time and, after
 * the meeting, plays the video recording from there, as a key frame does. See
 * docs/meeting-video.md.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Container, Header } from '@cloudscape-design/components';

import './MeetingChapters.css';

const MeetingChapters = ({ chapters, onSelect = undefined, canPlayRecording = false }) => {
  if (!chapters?.length) return null;
  return (
    <Container
      header={
        <Header
          variant="h4"
          counter={`(${chapters.length})`}
          description={
            canPlayRecording
              ? 'Select a chapter to move the transcript there and play the recording from it.'
              : 'Select a chapter to move the transcript there.'
          }
        >
          Chapters
        </Header>
      }
    >
      <ol className="meeting-chapters" data-testid="meeting-chapters">
        {chapters.map((chapter) => (
          <li key={chapter.seconds}>
            <Box variant="span" color="text-body-secondary" fontSize="body-s">
              <span className="meeting-chapters-time">{chapter.label}</span>
            </Box>
            {onSelect ? (
              <Button variant="inline-link" onClick={() => onSelect(chapter)}>
                {chapter.title}
              </Button>
            ) : (
              <span>{chapter.title}</span>
            )}
          </li>
        ))}
      </ol>
    </Container>
  );
};

MeetingChapters.propTypes = {
  chapters: PropTypes.arrayOf(
    PropTypes.shape({
      seconds: PropTypes.number.isRequired,
      label: PropTypes.string.isRequired,
      title: PropTypes.string.isRequired,
    }),
  ).isRequired,
  onSelect: PropTypes.func,
  canPlayRecording: PropTypes.bool,
};

export default MeetingChapters;
