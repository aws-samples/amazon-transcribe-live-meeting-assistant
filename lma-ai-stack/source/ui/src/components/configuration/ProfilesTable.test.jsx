/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The profile list on its own: what each selection offers, what each action
 * is called with, and the filter.
 */
import React, { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ProfilesTable, { deleteProblem } from './ProfilesTable';

const PROFILES = [
  { name: 'default', managed: true, active: false, description: 'Deployment default', updatedAt: null },
  { name: 'custom', managed: false, active: true, description: 'What meetings use', updatedAt: null },
  { name: 'trial', managed: false, active: false, description: 'Trying a new model', updatedAt: null },
  { name: 'pilot', managed: false, active: false, description: '', updatedAt: null },
];

// The table with its selection held as the workspace holds it.
const Harness = (handlers) => {
  const [selected, setSelected] = useState([]);
  return (
    <ProfilesTable
      profiles={PROFILES}
      openProfile="custom"
      selected={selected}
      onSelectionChange={setSelected}
      onOpen={vi.fn()}
      onCompare={vi.fn()}
      onActivate={vi.fn()}
      onImport={vi.fn()}
      onDelete={vi.fn()}
      onCreate={vi.fn()}
      onRefresh={vi.fn()}
      // eslint-disable-next-line react/jsx-props-no-spreading -- each test's own handlers
      {...handlers}
    />
  );
};

const select = (name) => fireEvent.click(screen.getByRole('checkbox', { name: `Select profile ${name}` }));

describe('ProfilesTable', () => {
  it('opens a profile by its name', () => {
    const onOpen = vi.fn();
    render(<Harness onOpen={onOpen} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open trial' }));
    expect(onOpen).toHaveBeenCalledWith('trial');
  });

  it('compares the profiles selected, in the order they are listed', () => {
    const onCompare = vi.fn();
    render(<Harness onCompare={onCompare} />);
    select('trial');
    select('default');
    select('pilot');
    const compare = screen.getByRole('button', { name: 'Compare profiles (3)' });
    fireEvent.click(compare);
    expect(onCompare).toHaveBeenCalledTimes(1);
  });

  it('activates and deletes only a selection those actions can work on', () => {
    const onActivate = vi.fn();
    const onDelete = vi.fn();
    render(<Harness onActivate={onActivate} onDelete={onDelete} />);
    select('trial');
    fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
    expect(onActivate).toHaveBeenCalledWith('trial');
    select('pilot');
    expect(screen.getByRole('button', { name: 'Activate' }).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onDelete).toHaveBeenCalledWith(['trial', 'pilot']);
  });

  it('finds profiles by name or description', () => {
    render(<Harness />);
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a profile' }), { target: { value: 'new model' } });
    expect(screen.getByRole('button', { name: 'Open trial' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open custom' })).toBeNull();
    expect(screen.getAllByText('1 match').length).toBeGreaterThan(0);
  });

  it('marks the active profile and the one open', () => {
    render(<Harness />);
    const row = screen.getByRole('button', { name: 'Open custom' }).closest('tr');
    expect(within(row).getByText('Active')).toBeTruthy();
    expect(within(row).getByText('Open in the editor')).toBeTruthy();
    expect(
      within(screen.getByRole('button', { name: 'Open default' }).closest('tr')).getByText('Managed'),
    ).toBeTruthy();
  });
});

describe('deleteProblem', () => {
  const byName = (names) => PROFILES.filter((entry) => names.includes(entry.name));

  it('allows Custom profiles that are not active', () => {
    expect(deleteProblem(byName(['trial', 'pilot']))).toBeNull();
  });

  it('refuses the active profile, then a managed one, saying which', () => {
    expect(deleteProblem(byName(['trial', 'custom']))).toBe('Activate another profile before deleting custom.');
    expect(deleteProblem(byName(['trial', 'default']))).toBe('The deployment manages default.');
    expect(deleteProblem([])).toBe('Select the profiles to delete.');
  });
});
