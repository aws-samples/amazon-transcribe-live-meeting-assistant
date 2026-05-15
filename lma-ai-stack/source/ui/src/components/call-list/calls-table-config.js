/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import {
  Button,
  ButtonDropdown,
  CollectionPreferences,
  Icon,
  Link,
  SpaceBetween,
  StatusIndicator,
  Popover,
} from '@cloudscape-design/components';

import rehypeRaw from 'rehype-raw';
import ReactMarkdown from 'react-markdown';
import { TableHeader } from '../common/table';
import { CALLS_PATH } from '../../routes/constants';
import { SentimentIndicator } from '../sentiment-icon/SentimentIcon';
import { SentimentTrendIndicator } from '../sentiment-trend-icon/SentimentTrendIcon';
import { CategoryAlertPill } from './CategoryAlertPill';
import { CategoryPills } from './CategoryPills';
import { getTextOnlySummary } from '../common/summary';
import { shareModal, deleteModal } from '../common/meeting-controls';
import CustomDateRangeModal from './CustomDateRangeModal';

export const KEY_COLUMN_ID = 'callId';

export const COLUMN_DEFINITIONS_MAIN = [
  {
    id: KEY_COLUMN_ID,
    header: 'Meeting ID',
    cell: (item) => <Link href={`#${CALLS_PATH}/${encodeURIComponent(item.callId)}`}>{item.callId}</Link>,
    sortingField: 'callId',
    width: 325,
  },
  {
    id: 'alerts',
    header: '⚠',
    cell: (item) => <CategoryAlertPill alertCount={item.alertCount} categories={item.callCategories} />,
    sortingField: 'alertCount',
    width: 85,
  },
  {
    id: 'agentId',
    header: 'Owner Name',
    cell: (item) => item.agentId,
    sortingField: 'agentId',
  },
  {
    id: 'initiationTimeStamp',
    header: 'Initiation Timestamp',
    cell: (item) => item.initiationTimeStamp,
    sortingField: 'initiationTimeStamp',
    isDescending: false,
    width: 225,
  },
  {
    id: 'owner',
    header: 'Owner Email',
    cell: (item) => item.owner,
    sortingField: 'owner',
  },
  {
    id: 'sharedWith',
    header: 'Shared With',
    cell: (item) => item.sharedWith,
    sortingField: 'sharedWith',
  },
  {
    id: 'summary',
    header: 'Summary',
    cell: (item) => {
      const summary = getTextOnlySummary(item.callSummaryText);
      return (
        <Popover
          dismissButton={false}
          position="top"
          size="large"
          triggerType="text"
          content={<ReactMarkdown rehypePlugins={[rehypeRaw]}>{summary ?? ''}</ReactMarkdown>}
        >
          {summary && summary.length > 20 ? `${summary.substring(0, 20)}...` : summary}
        </Popover>
      );
    },
    sortingField: 'summary',
  },
  {
    id: 'callerPhoneNumber',
    header: 'Caller Phone Number',
    cell: (item) => item.callerPhoneNumber,
    sortingField: 'callerPhoneNumber',
    width: 175,
  },
  {
    id: 'recordingStatus',
    header: 'Status',
    cell: (item) => (
      <StatusIndicator type={item.recordingStatusIcon}>{` ${item.recordingStatusLabel} `}</StatusIndicator>
    ),
    sortingField: 'recordingStatusLabel',
    width: 150,
  },
  {
    id: 'callerSentiment',
    header: 'Caller Sentiment',
    cell: (item) => <SentimentIndicator sentiment={item?.callerSentimentLabel} />,
    sortingField: 'callerSentimentLabel',
  },
  {
    id: 'callerSentimentTrend',
    header: 'Caller Sentiment Trend',
    cell: (item) => <SentimentTrendIndicator trend={item?.callerSentimentTrendLabel} />,
    sortingField: 'callerSentimentTrendLabel',
  },
  {
    id: 'agentSentiment',
    header: 'Agent Sentiment',
    cell: (item) => <SentimentIndicator sentiment={item?.agentSentimentLabel} />,
    sortingField: 'agentSentimentLabel',
  },
  {
    id: 'agentSentimentTrend',
    header: 'Agent Sentiment Trend',
    cell: (item) => <SentimentTrendIndicator trend={item?.agentSentimentTrendLabel} />,
    sortingField: 'agentSentimentTrendLabel',
  },
  {
    id: 'conversationDuration',
    header: 'Duration',
    cell: (item) => item.conversationDurationTimeStamp,
    sortingField: 'conversationDurationTimeStamp',
  },
  {
    id: 'menu',
    header: '',
    cell: (item) => (
      <ButtonDropdown
        items={[
          {
            text: 'Open in PCA',
            href: item.pcaUrl,
            external: true,
            disabled: !item.pcaUrl,
            externalIconAriaLabel: '(opens in new tab)',
          },
        ]}
        expandToViewport
      >
        <Icon name="menu" />
      </ButtonDropdown>
    ),
    width: 120,
  },
  {
    id: 'callCategories',
    header: 'Categories',
    cell: (item) => <CategoryPills categories={item.callCategories} />,
    sortingField: 'callCategoryCount',
    width: 200,
  },
];

export const DEFAULT_SORT_COLUMN = COLUMN_DEFINITIONS_MAIN[3];

export const SELECTION_LABELS = {
  itemSelectionLabel: (data, row) => `select ${row.callId}`,
  allItemsSelectionLabel: () => 'select all',
  selectionGroupLabel: 'Meeting selection',
};

const PAGE_SIZE_OPTIONS = [
  { value: 10, label: '10 Meetings' },
  { value: 30, label: '30 Meetings' },
  { value: 50, label: '50 Meetings' },
];

const VISIBLE_CONTENT_OPTIONS = [
  {
    label: 'Meeting list properties',
    options: [
      { id: 'callId', label: 'Meeting ID', editable: false },
      { id: 'agentId', label: 'Name' },
      { id: 'owner', label: 'Owner' },
      { id: 'sharedWith', label: 'Shared With' },
      { id: 'initiationTimeStamp', label: 'Initiation Timestamp' },
      { id: 'recordingStatus', label: 'Status' },
      { id: 'summary', label: 'Summary' },
      { id: 'conversationDuration', label: 'Duration' },
    ],
  },
];

const VISIBLE_CONTENT = [
  'agentId',
  'owner',
  'sharedWith',
  'initiationTimeStamp',
  'recordingStatus',
  'summary',
  'conversationDuration',
];

export const DEFAULT_PREFERENCES = {
  pageSize: PAGE_SIZE_OPTIONS[0].value,
  visibleContent: VISIBLE_CONTENT,
  wraplines: false,
};

/* eslint-disable react/prop-types, react/jsx-props-no-spreading */
export const CallsPreferences = ({
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

/// ///////////////////////////////////////////////////////////////////////////
// Date-range selector for the meeting list
/// ///////////////////////////////////////////////////////////////////////////
// The meeting list is now scoped by an ISO-8601 date range, queried via
// `listCallsDateRange` backed by the TypeDateIndex GSI on EventSourcingTable.
// We expose a set of relative presets (last N hours/days) plus a "Custom…"
// option that opens a CloudScape DateRangePicker modal.

export const DEFAULT_DATE_RANGE_HOURS = 8;
export const DATE_RANGE_STORAGE_KEY = 'callListDateRange';

// Preset presets used by the Load dropdown.  Each entry resolves to an
// ISO-8601 start/end pair at click time (so "Last 2 hrs" is always
// relative to now).
const TIME_PERIOD_DROPDOWN_CONFIG = {
  'refresh-2h': { hours: 2, text: '2 hrs' },
  'refresh-4h': { hours: 4, text: '4 hrs' },
  'refresh-8h': { hours: 8, text: '8 hrs' },
  'refresh-1d': { hours: 24, text: '1 day' },
  'refresh-2d': { hours: 48, text: '2 days' },
  'refresh-1w': { hours: 24 * 7, text: '1 week' },
  'refresh-2w': { hours: 24 * 14, text: '2 weeks' },
  'refresh-1m': { hours: 24 * 30, text: '30 days' },
};
const TIME_PERIOD_DROPDOWN_ITEMS = [
  ...Object.keys(TIME_PERIOD_DROPDOWN_CONFIG).map((k) => ({
    id: k,
    text: TIME_PERIOD_DROPDOWN_CONFIG[k].text,
  })),
  { id: 'refresh-custom', text: 'Custom…' },
];

/** Turn a number of hours into a {startDateIso, endDateIso, hours} window
 *  ending at the current instant.
 */
export const hoursToDateRange = (hours) => {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 3600 * 1000);
  return {
    startDateIso: start.toISOString(),
    endDateIso: end.toISOString(),
    hours,
  };
};

/**
 * Load + persist the last-used date range from localStorage.  Returns
 * undefined if nothing is cached, so the hook falls back to DEFAULT_DATE_RANGE_HOURS.
 * If the cache is a pure "relative" window (has an `hours` key) we recompute
 * the absolute bounds on read so the user always sees up-to-the-second data
 * on reload.
 */
export const loadCachedDateRange = () => {
  try {
    const raw = localStorage.getItem(DATE_RANGE_STORAGE_KEY);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw);
    if (parsed?.hours && typeof parsed.hours === 'number') {
      return hoursToDateRange(parsed.hours);
    }
    if (parsed?.startDateIso && parsed?.endDateIso) {
      return parsed;
    }
    return undefined;
  } catch (e) {
    return undefined;
  }
};

export const persistDateRange = (range) => {
  try {
    // If it's a relative range we only persist the hours so reloads
    // recompute a fresh absolute window.
    if (range?.hours) {
      localStorage.setItem(DATE_RANGE_STORAGE_KEY, JSON.stringify({ hours: range.hours }));
    } else if (range?.startDateIso && range?.endDateIso) {
      localStorage.setItem(DATE_RANGE_STORAGE_KEY, JSON.stringify(range));
    }
  } catch (e) {
    // ignore quota/privacy-mode errors
  }
};

const formatShortLabel = (range) => {
  if (!range) return '';
  if (range.hours) {
    const preset = Object.values(TIME_PERIOD_DROPDOWN_CONFIG).find((c) => c.hours === range.hours);
    if (preset) return preset.text;
    return `${range.hours} hr${range.hours === 1 ? '' : 's'}`;
  }
  const start = new Date(range.startDateIso);
  const end = new Date(range.endDateIso);
  const fmt = (d) => `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  return `Custom: ${fmt(start)} – ${fmt(end)}`;
};

export const CallsCommonHeader = ({ resourceName = 'Meetings', ...props }) => {
  const [customModalVisible, setCustomModalVisible] = useState(false);

  const onPeriodToLoadChange = ({ detail }) => {
    const { id } = detail;
    if (id === 'refresh-custom') {
      setCustomModalVisible(true);
      return;
    }
    const cfg = TIME_PERIOD_DROPDOWN_CONFIG[id];
    if (!cfg) return;
    const range = hoursToDateRange(cfg.hours);
    props.setDateRange(range);
    persistDateRange(range);
  };

  const onCustomApply = ({ startDateIso, endDateIso }) => {
    const range = { startDateIso, endDateIso };
    props.setDateRange(range);
    persistDateRange(range);
    setCustomModalVisible(false);
  };

  const rangeLabel = formatShortLabel(props.dateRange);

  // Header counter: show "(loaded / total)" when we know the server-side
  // total and it exceeds what we've materialised client-side.  Otherwise
  // fall back to the default "(loaded)" / "(loaded / selected)" rendered
  // by TableHeader from totalItems + selectedItems.
  const loadedCount = props.loadedCallCount ?? (props.calls ? props.calls.length : undefined);
  const selectedCount = props.selectedItems?.length || 0;
  let counter;
  if (typeof props.totalCallCount === 'number' && typeof loadedCount === 'number') {
    const totalLabel = props.totalCallCountTruncated ? `${props.totalCallCount}+` : `${props.totalCallCount}`;
    if (props.totalCallCount > loadedCount) {
      counter = selectedCount
        ? `(${selectedCount}/${loadedCount} loaded of ${totalLabel})`
        : `(${loadedCount} loaded of ${totalLabel})`;
    } else {
      counter = selectedCount ? `(${selectedCount}/${totalLabel})` : `(${totalLabel})`;
    }
  }

  return (
    <>
      <TableHeader
        title={resourceName}
        counter={counter}
        actionButtons={
          <SpaceBetween size="xxs" direction="horizontal">
            <ButtonDropdown
              loading={props.loading}
              onItemClick={onPeriodToLoadChange}
              items={TIME_PERIOD_DROPDOWN_ITEMS}
            >
              {`Load: ${rangeLabel}`}
            </ButtonDropdown>
            <Button
              iconName="refresh"
              variant="normal"
              loading={props.loading}
              onClick={() => props.setIsLoading(true)}
            />
            <Button
              iconName="download"
              variant="normal"
              loading={props.loading}
              onClick={() => props.downloadToExcel()}
            />
            {shareModal(props)}
            {deleteModal(props)}
          </SpaceBetween>
        }
        {...props}
      />
      <CustomDateRangeModal
        visible={customModalVisible}
        onDismiss={() => setCustomModalVisible(false)}
        onApply={onCustomApply}
      />
    </>
  );
};
