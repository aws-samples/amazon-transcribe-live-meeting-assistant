/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/** The chapters list: one entry per chapter, selectable, and nothing without chapters. */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import MeetingChapters from './MeetingChapters';

const chapters = [
  { seconds: 0, label: '00:00', title: 'Welcome' },
  { seconds: 312, label: '05:12', title: 'Budget review' },
];

describe('MeetingChapters', () => {
  it('lists the chapters with their times and passes the one selected', () => {
    const onSelect = vi.fn();
    render(<MeetingChapters chapters={chapters} onSelect={onSelect} />);
    expect(screen.getByText('05:12')).toBeTruthy();
    fireEvent.click(screen.getByText('Budget review'));
    expect(onSelect).toHaveBeenCalledWith(chapters[1]);
  });

  it('renders nothing for a meeting without chapters', () => {
    const { container } = render(<MeetingChapters chapters={[]} />);
    expect(container.innerHTML).toBe('');
  });
});
