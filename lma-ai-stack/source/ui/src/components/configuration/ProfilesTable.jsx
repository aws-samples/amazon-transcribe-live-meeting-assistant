/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useMemo } from 'react';
import PropTypes from 'prop-types';
import { Badge, Box, Button, Header, Pagination, SpaceBetween, Table, TextFilter } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';

import { DEFAULT_PROFILE } from './configuration-api';

// What a profile is called in the table and in the dialogs about one.
export const profileText = (entry) => (entry.name === DEFAULT_PROFILE ? 'default (deployment default)' : entry.name);

const whenText = (iso) => {
  const parsed = iso ? new Date(iso) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : iso || '—';
};

const BADGE_TITLES = {
  managed: 'Managed: rewritten by every stack update, so it cannot be saved. Copy it to change a setting.',
  custom: 'Custom: a profile an admin created, which can be changed, saved and deleted.',
  active: 'Active: the profile every meeting uses.',
};

/**
 * Why the selected profiles cannot be deleted, or null when they can: the
 * active profile and the Managed ones are refused by the store, so the button
 * says so instead.
 */
export const deleteProblem = (selected) => {
  if (!selected.length) return 'Select the profiles to delete.';
  const active = selected.filter((entry) => entry.active);
  if (active.length) return `Activate another profile before deleting ${active[0].name}.`;
  const managed = selected.filter((entry) => entry.managed || entry.name === DEFAULT_PROFILE);
  if (managed.length) return `The deployment manages ${managed.map((entry) => entry.name).join(', ')}.`;
  return null;
};

// The table's columns. Made outside the component, so each cell is a render of
// the same function rather than a component type made anew on every render.
const profileColumns = (openProfile, onOpen) => [
  {
    id: 'name',
    header: 'Name',
    cell: (entry) => {
      const open = entry.name === openProfile;
      return (
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Box fontWeight={open || entry.active ? 'bold' : 'normal'}>
            <Button variant="inline-link" ariaLabel={`Open ${entry.name}`} onClick={() => onOpen(entry.name)}>
              {profileText(entry)}
            </Button>
          </Box>
          {entry.active && (
            <span title={BADGE_TITLES.active}>
              <Badge color="green">Active</Badge>
            </span>
          )}
          {open && (
            <Box variant="small" color="text-status-info">
              Open in the editor
            </Box>
          )}
        </SpaceBetween>
      );
    },
    sortingField: 'name',
  },
  {
    id: 'type',
    header: 'Type',
    cell: (entry) =>
      entry.managed ? (
        <span title={BADGE_TITLES.managed}>
          <Badge color="blue">Managed</Badge>
        </span>
      ) : (
        <span title={BADGE_TITLES.custom}>
          <Badge color="grey">Custom</Badge>
        </span>
      ),
    sortingComparator: (a, b) => Number(b.managed) - Number(a.managed),
    width: 120,
  },
  { id: 'description', header: 'Description', cell: (entry) => entry.description || '—' },
  {
    id: 'updated',
    header: 'Updated',
    cell: (entry) =>
      entry.updatedBy ? `${whenText(entry.updatedAt)} by ${entry.updatedBy}` : whenText(entry.updatedAt),
    sortingField: 'updatedAt',
  },
];

/**
 * The configuration profiles, as a table above the editor: which one is
 * active, which one is open, and the actions that work on whole profiles.
 * Selecting rows is for those actions; following a profile's name opens it.
 */
const ProfilesTable = ({
  profiles,
  loading = false,
  busy = false,
  openProfile = null,
  selected,
  onSelectionChange,
  onOpen,
  onCompare,
  onActivate,
  onImport,
  onDelete,
  onCreate,
  onRefresh,
}) => {
  const columns = profileColumns(openProfile, onOpen);

  const { items, collectionProps, filterProps, filteredItemsCount, paginationProps } = useCollection(profiles, {
    filtering: {
      filteringFunction: (entry, text) => {
        const wanted = text.trim().toLowerCase();
        return !wanted || [entry.name, entry.description].some((value) => value.toLowerCase().includes(wanted));
      },
      empty: <Box textAlign="center">There are no configuration profiles.</Box>,
      noMatch: <Box textAlign="center">No profile matches the filter.</Box>,
    },
    pagination: { pageSize: 10 },
    sorting: {},
  });

  const chosen = useMemo(() => profiles.filter((entry) => selected.includes(entry.name)), [profiles, selected]);
  const single = chosen.length === 1 ? chosen[0] : null;
  const cannotDelete = deleteProblem(chosen);

  /* eslint-disable react/jsx-props-no-spreading -- the collection hook's props, as CallList passes them */
  return (
    <Table
      {...collectionProps}
      items={items}
      columnDefinitions={columns}
      trackBy="name"
      selectionType="multi"
      selectedItems={chosen}
      onSelectionChange={({ detail }) => onSelectionChange(detail.selectedItems.map((entry) => entry.name))}
      ariaLabels={{
        selectionGroupLabel: 'Profile selection',
        allItemsSelectionLabel: () => 'Select all profiles',
        itemSelectionLabel: (_selection, entry) => `Select profile ${entry.name}`,
      }}
      loading={loading}
      loadingText="Loading the configuration profiles"
      variant="embedded"
      contentDensity="compact"
      wrapLines
      filter={
        <TextFilter
          {...filterProps}
          filteringPlaceholder="Find a profile"
          filteringAriaLabel="Find a profile"
          countText={`${filteredItemsCount} ${filteredItemsCount === 1 ? 'match' : 'matches'}`}
        />
      }
      pagination={<Pagination {...paginationProps} />}
      header={
        <Header
          counter={loading ? undefined : `(${profiles.length})`}
          description={
            'The active profile is what every meeting uses. Open a profile to view or edit it below; select ' +
            'profiles to compare, activate or delete them.'
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                iconName="refresh"
                onClick={onRefresh}
                disabled={loading || busy}
                ariaLabel="Refresh the profiles"
              />
              <Button onClick={onCompare} disabled={chosen.length < 2 || busy}>
                {`Compare profiles (${chosen.length})`}
              </Button>
              <Button
                onClick={() => onActivate(single.name)}
                disabled={!single || single.active || busy}
                disabledReason={single?.active ? `${single.name} is already the active profile.` : undefined}
              >
                Activate
              </Button>
              <Button iconName="upload" onClick={onImport} disabled={busy}>
                Import
              </Button>
              <Button
                onClick={() => onDelete(chosen.map((entry) => entry.name))}
                disabled={Boolean(cannotDelete) || busy}
                disabledReason={chosen.length ? cannotDelete || undefined : undefined}
              >
                Delete
              </Button>
              <Button variant="primary" iconName="add-plus" onClick={onCreate} disabled={busy}>
                Create profile
              </Button>
            </SpaceBetween>
          }
        >
          Profiles
        </Header>
      }
    />
  );
  /* eslint-enable react/jsx-props-no-spreading */
};

ProfilesTable.propTypes = {
  profiles: PropTypes.arrayOf(
    PropTypes.shape({
      name: PropTypes.string.isRequired,
      managed: PropTypes.bool,
      active: PropTypes.bool,
      description: PropTypes.string,
      updatedAt: PropTypes.string,
      updatedBy: PropTypes.string,
    }),
  ).isRequired,
  loading: PropTypes.bool,
  busy: PropTypes.bool,
  // The profile open in the editor below the table.
  openProfile: PropTypes.string,
  // The names of the selected profiles.
  selected: PropTypes.arrayOf(PropTypes.string).isRequired,
  onSelectionChange: PropTypes.func.isRequired,
  onOpen: PropTypes.func.isRequired,
  onCompare: PropTypes.func.isRequired,
  onActivate: PropTypes.func.isRequired,
  onImport: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
  onCreate: PropTypes.func.isRequired,
  onRefresh: PropTypes.func.isRequired,
};

export default ProfilesTable;
