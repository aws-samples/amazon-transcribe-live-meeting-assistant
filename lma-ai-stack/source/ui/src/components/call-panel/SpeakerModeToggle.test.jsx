/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import SpeakerModeToggle from './SpeakerModeToggle';
import { ONE_PERSON, REASON_DEFAULT, REASON_ROOM_NAME, SEVERAL_PEOPLE } from '../common/speaker-modes';

const BOB = { key: 'REMOTE#Bob Strahan', base: 'Bob Strahan', mode: ONE_PERSON, reason: REASON_DEFAULT };
const ROOM = { key: 'REMOTE#ConfRm_1', base: 'ConfRm_1', mode: SEVERAL_PEOPLE, reason: REASON_ROOM_NAME };

const toggleFor = (base) => screen.getByRole('button', { name: `Show ${base} as several people` });

describe('SpeakerModeToggle', () => {
  it('is one icon button, not pressed for a name shown as one person and pressed for several', () => {
    const { unmount } = render(<SpeakerModeToggle group={BOB} onSetMode={vi.fn()} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(toggleFor('Bob Strahan').getAttribute('aria-pressed')).toBe('false');
    expect(toggleFor('Bob Strahan').textContent).toBe('');
    unmount();
    render(<SpeakerModeToggle group={ROOM} onSetMode={vi.fn()} />);
    expect(toggleFor('ConfRm_1').getAttribute('aria-pressed')).toBe('true');
  });

  it('switches to several people with one click, and back to one person', () => {
    const onSetMode = vi.fn();
    const { unmount } = render(<SpeakerModeToggle group={BOB} onSetMode={onSetMode} />);
    fireEvent.click(toggleFor('Bob Strahan'));
    expect(onSetMode).toHaveBeenLastCalledWith('REMOTE#Bob Strahan', SEVERAL_PEOPLE);
    unmount();
    render(<SpeakerModeToggle group={ROOM} onSetMode={onSetMode} />);
    fireEvent.click(toggleFor('ConfRm_1'));
    expect(onSetMode).toHaveBeenLastCalledWith('REMOTE#ConfRm_1', ONE_PERSON);
  });

  it('explains the current mode and what a click does on hover', () => {
    render(<SpeakerModeToggle group={BOB} onSetMode={vi.fn()} />);
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.mouseEnter(screen.getByTestId('speaker-mode-toggle'));
    const tip = screen.getByRole('tooltip');
    expect(tip.textContent).toContain('One person: every voice under "Bob Strahan" is shown as one speaker.');
    expect(tip.textContent).toContain('Click if several people share this name.');
    fireEvent.mouseLeave(screen.getByTestId('speaker-mode-toggle'));
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('shows the explanation on keyboard focus, and gives it as the accessible description', () => {
    render(<SpeakerModeToggle group={ROOM} onSetMode={vi.fn()} />);
    const button = toggleFor('ConfRm_1');
    fireEvent.focus(button);
    expect(screen.getByRole('tooltip').textContent).toContain('Click to show them as one person.');
    const description = document.getElementById(button.getAttribute('aria-describedby'));
    expect(description.textContent).toContain('Several people: each voice under "ConfRm_1" is shown on its own.');
    expect(description.textContent).toContain('reads as a room');
    fireEvent.blur(button);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('is disabled for a viewer who may not change it, and its tooltip offers no click', () => {
    render(<SpeakerModeToggle group={BOB} />);
    expect(toggleFor('Bob Strahan').disabled).toBe(true);
    fireEvent.mouseEnter(screen.getByTestId('speaker-mode-toggle'));
    expect(screen.getByRole('tooltip').textContent).not.toContain('Click');
  });
});
