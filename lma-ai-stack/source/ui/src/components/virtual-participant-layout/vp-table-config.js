/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import {
  Button,
  ButtonDropdown,
  CollectionPreferences,
  Icon,
  SpaceBetween,
  Badge,
  Popover,
} from '@awsui/components-react';
import { Link as RouterLink } from 'react-router-dom';

import { TableHeader } from '../common/table';
import { exportToExcel } from '../common/download-func';

export const KEY_COLUMN_ID = 'id';

// Status badge component for VP status
const StatusBadge = ({ status }) => {
  const getStatusProps = (vpStatus) => {
    switch (vpStatus) {
      case 'SCHEDULED':
        return { color: 'blue', children: 'Scheduled' };
      case 'INITIALIZING':
        return { color: 'blue', children: 'Initializing' };
      case 'CONNECTING':
        return { color: 'blue', children: 'Connecting' };
      case 'JOINING':
        return { color: 'blue', children: 'Joining' };
      case 'JOINED':
        return { color: 'green', children: 'Joined' };
      case 'ACTIVE':
        return { color: 'green', children: 'Active' };
      case 'COMPLETED':
        return { color: 'green', children: 'Completed' };
      case 'FAILED':
        return { color: 'red', children: 'Failed' };
      case 'ENDED':
        return { color: 'grey', children: 'Ended' };
      case 'CANCELLED':
        return { color: 'grey', children: 'Cancelled' };
      default:
        return { color: 'grey', children: vpStatus || 'Unknown' };
    }
  };

  const statusProps = getStatusProps(status);
  return <Badge color={statusProps.color}>{statusProps.children}</Badge>;
};

StatusBadge.propTypes = {
  status: PropTypes.string.isRequired,
};

// Render function for meeting name cell with link
const renderMeetingNameCell = (item) => (
  <RouterLink to={`/virtual-participant/${item.id}`} style={{ textDecoration: 'none', color: '#0972d3' }}>
    {item.meetingName}
  </RouterLink>
);

// Render function for shared with cell with smart truncation
const renderSharedWithCell = (item) => {
  const sharedWith = item.SharedWith || '';
  if (!sharedWith) return '-';

  const emails = sharedWith
    .split(',')
    .map((email) => email.trim())
    .filter((email) => email);

  if (emails.length === 0) return '-';
  if (emails.length === 1) return emails[0];
  if (emails.length <= 3) return emails.join(', ');

  const displayEmails = emails.slice(0, 2).join(', ');
  const remainingCount = emails.length - 2;

  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="text"
      content={
        <div>
          <strong>Shared with:</strong>
          <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
            {emails.map((email) => (
              <li key={email}>{email}</li>
            ))}
          </ul>
        </div>
      }
    >
      {`${displayEmails} +${remainingCount} more`}
    </Popover>
  );
};

export const COLUMN_DEFINITIONS_MAIN = [
  {
    id: KEY_COLUMN_ID,
    header: 'VP ID',
    cell: (item) => item.id,
    sortingField: 'id',
    width: 200,
  },
  {
    id: 'meetingName',
    header: 'Meeting Name',
    cell: renderMeetingNameCell,
    sortingField: 'meetingName',
    width: 250,
  },
  {
    id: 'meetingPlatform',
    header: 'Platform',
    cell: (item) => item.meetingPlatform,
    sortingField: 'meetingPlatform',
    width: 100,
  },
  {
    id: 'meetingId',
    header: 'Meeting ID',
    cell: (item) => item.meetingId,
    sortingField: 'meetingId',
    width: 150,
  },
  {
    id: 'status',
    header: 'Status',
    cell: (item) => <StatusBadge status={item.status} />,
    sortingField: 'status',
    width: 120,
  },
  {
    id: 'owner',
    header: 'Owner Email',
    cell: (item) => item.Owner || item.owner || '-',
    sortingField: 'Owner',
    width: 200,
  },
  {
    id: 'sharedWith',
    header: 'Shared With',
    cell: renderSharedWithCell,
    sortingField: 'SharedWith',
    width: 200,
  },
  {
    id: 'scheduledFor',
    header: 'Scheduled For',
    cell: (item) => {
      if (item.isScheduled && item.scheduledFor) {
        return new Date(item.scheduledFor).toLocaleString();
      }
      if (item.meetingTime) {
        return new Date(item.meetingTime * 1000).toLocaleString();
      }
      return '-';
    },
    sortingField: 'scheduledFor',
    width: 180,
  },
  {
    id: 'createdAt',
    header: 'Created',
    cell: (item) => new Date(item.createdAt).toLocaleString(),
    sortingField: 'createdAt',
    isDescending: true,
    width: 180,
  },
  {
    id: 'updatedAt',
    header: 'Last Updated',
    cell: (item) => new Date(item.updatedAt).toLocaleString(),
    sortingField: 'updatedAt',
    width: 180,
  },
  {
    id: 'menu',
    header: '',
    cell: (item) => (
      <ButtonDropdown
        items={[
          {
            text: 'View Details',
            href: `#/virtual-participant/${item.id}`,
          },
          {
            text: 'End Participant',
            disabled: !['ACTIVE', 'JOINED', 'CONNECTING', 'JOINING'].includes(item.status),
          },
        ]}
        expandToViewport
      >
        <Icon name="menu" />
      </ButtonDropdown>
    ),
    width: 80,
  },
];

export const DEFAULT_SORT_COLUMN = COLUMN_DEFINITIONS_MAIN[8]; // createdAt column

export const SELECTION_LABELS = {
  itemSelectionLabel: (data, row) => `select ${row.meetingName}`,
  allItemsSelectionLabel: () => 'select all',
  selectionGroupLabel: 'Virtual Participant selection',
};

const PAGE_SIZE_OPTIONS = [
  { value: 10, label: '10 Virtual Participants' },
  { value: 30, label: '30 Virtual Participants' },
  { value: 50, label: '50 Virtual Participants' },
];

const VISIBLE_CONTENT_OPTIONS = [
  {
    label: 'Virtual Participant properties',
    options: [
      { id: 'id', label: 'VP ID', editable: false },
      { id: 'meetingName', label: 'Meeting Name' },
      { id: 'meetingPlatform', label: 'Platform' },
      { id: 'meetingId', label: 'Meeting ID' },
      { id: 'status', label: 'Status' },
      { id: 'owner', label: 'Owner Email' },
      { id: 'sharedWith', label: 'Shared With' },
      { id: 'scheduledFor', label: 'Scheduled For' },
      { id: 'createdAt', label: 'Created' },
      { id: 'updatedAt', label: 'Last Updated' },
    ],
  },
];

const VISIBLE_CONTENT = [
  'meetingName',
  'meetingPlatform',
  'meetingId',
  'status',
  'owner',
  'sharedWith',
  'scheduledFor',
  'createdAt',
];

export const DEFAULT_PREFERENCES = {
  pageSize: PAGE_SIZE_OPTIONS[0].value,
  visibleContent: VISIBLE_CONTENT,
  wrapLines: false,
};

/* eslint-disable react/prop-types, react/jsx-props-no-spreading */
export const VPPreferences = ({
  preferences,
  setPreferences,
  disabled,
  pageSizeOptions = PAGE_SIZE_OPTIONS,
  visibleContentOptions = VISIBLE_CONTENT_OPTIONS,
}) => (
  <CollectionPreferences
    title="Preferences"
    confirmLabel="Confirm"
    cancelLabel="Cancel"
    disabled={disabled}
    preferences={preferences}
    onConfirm={({ detail }) => setPreferences(detail)}
    pageSizePreference={{
      title: 'Page size',
      options: pageSizeOptions,
    }}
    wrapLinesPreference={{
      label: 'Wrap lines',
      description: 'Check to see all the text and wrap the lines',
    }}
    visibleContentPreference={{
      title: 'Select visible columns',
      options: visibleContentOptions,
    }}
  />
);

// Time-based filtering for Virtual Participants
const TIME_PERIOD_DROPDOWN_CONFIG = {
  'refresh-2h': { hours: 2, text: '2 hrs' },
  'refresh-4h': { hours: 4, text: '4 hrs' },
  'refresh-8h': { hours: 8, text: '8 hrs' },
  'refresh-1d': { hours: 24, text: '1 day' },
  'refresh-2d': { hours: 48, text: '2 days' },
  'refresh-1w': { hours: 168, text: '1 week' },
  'refresh-2w': { hours: 336, text: '2 weeks' },
  'refresh-1m': { hours: 720, text: '30 days' },
  'refresh-all': { hours: null, text: 'All time' },
};

const TIME_PERIOD_DROPDOWN_ITEMS = Object.keys(TIME_PERIOD_DROPDOWN_CONFIG).map((k) => ({
  id: k,
  ...TIME_PERIOD_DROPDOWN_CONFIG[k],
}));

// Local storage key to persist the last time filter
export const TIME_FILTER_STORAGE_KEY = 'vpTimeFilter';

export const VPCommonHeader = ({ resourceName = 'Virtual Participants', ...props }) => {
  const onTimeFilterChange = ({ detail }) => {
    const { id } = detail;
    const config = TIME_PERIOD_DROPDOWN_CONFIG[id];
    props.setTimeFilter(config.hours);
    localStorage.setItem(TIME_FILTER_STORAGE_KEY, JSON.stringify(config.hours));
  };

  // Find current time filter text
  const currentTimeFilter = TIME_PERIOD_DROPDOWN_ITEMS.find((item) => item.hours === props.timeFilter);
  const timeFilterText = currentTimeFilter?.text || 'All time';

  return (
    <TableHeader
      title={resourceName}
      actionButtons={
        <SpaceBetween size="xxs" direction="horizontal">
          <ButtonDropdown loading={props.loading} onItemClick={onTimeFilterChange} items={TIME_PERIOD_DROPDOWN_ITEMS}>
            {`Filter: ${timeFilterText}`}
          </ButtonDropdown>
          <Button iconName="refresh" variant="normal" loading={props.loading} onClick={() => props.onRefresh()} />
          <Button
            iconName="download"
            variant="normal"
            loading={props.loading}
            onClick={() => exportToExcel(props.items, 'Virtual-Participants-List')}
          />
          <Button variant="normal" onClick={() => props.onPasteInvite()}>
            Paste Meeting Invite
          </Button>
          <Button variant="primary" onClick={() => props.onCreateVP()}>
            Create Virtual Participant
          </Button>
        </SpaceBetween>
      }
      {...props}
    />
  );
};

// Helper function to filter VPs by time
export const filterVPsByTime = (participants, timeFilterHours) => {
  if (!timeFilterHours) return participants; // Show all if no filter

  const cutoffTime = new Date();
  cutoffTime.setHours(cutoffTime.getHours() - timeFilterHours);

  return participants.filter((vp) => {
    const createdAt = new Date(vp.createdAt);
    return createdAt >= cutoffTime;
  });
};
