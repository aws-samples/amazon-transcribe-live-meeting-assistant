/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { SentimentFluctuationChart } from './sentiment-charts';

const CALL_ID = 'Weekly sync-2026-11-03-14-05-09-123';
const line = (channel, endTime, sentimentWeighted) => ({ channel, endTime, sentimentWeighted });

describe('SentimentFluctuationChart', () => {
  it('charts the speech channels however many other channels the meeting holds', () => {
    const transcript = {
      [CALL_ID]: {
        AGENT_VOICETONE: { segments: [] },
        CALLER_VOICETONE: { segments: [] },
        AGENT_ASSISTANT: { segments: [] },
        MEETING_ASSISTANT: { segments: [] },
        CHAT_ASSISTANT: { segments: [] },
        MEETING_CHAT: { segments: [line('MEETING_CHAT', 3)] },
        REMOTE: { segments: [line('REMOTE', 5, 1.5)] },
        LOCAL: { segments: [line('LOCAL', 9, -1)] },
      },
    };
    const { container } = render(
      <SentimentFluctuationChart item={{ callId: CALL_ID }} callTranscriptPerCallId={transcript} />,
    );
    expect(container.textContent).toContain('Remote');
    expect(container.textContent).toContain('Local');
    ['MEETING_CHAT', 'MEETING_ASSISTANT', 'CHAT_ASSISTANT', 'n/a'].forEach((name) =>
      expect(container.textContent).not.toContain(name),
    );
  });
});
