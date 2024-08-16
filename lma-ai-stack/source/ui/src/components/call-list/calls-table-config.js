// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import {
  Button,
  ButtonDropdown,
  CollectionPreferences,
  Icon,
  Link,
  SpaceBetween,
  StatusIndicator,
  Popover,
} from '@awsui/components-react';

import rehypeRaw from 'rehype-raw';
import ReactMarkdown from 'react-markdown';
import { TableHeader } from '../common/table';
import { CALLS_PATH } from '../../routes/constants';
import { SentimentIndicator } from '../sentiment-icon/SentimentIcon';
import { SentimentTrendIndicator } from '../sentiment-trend-icon/SentimentTrendIcon';
import { CategoryAlertPill } from './CategoryAlertPill';
import { CategoryPills } from './CategoryPills';
import { getTextOnlySummary } from '../common/summary';

export const KEY_COLUMN_ID = 'callId';

export const COLUMN_DEFINITIONS_MAIN = [
  {
    id: KEY_COLUMN_ID,
    header: 'Meeting ID',
    cell: (item) => <Link href={`#${CALLS_PATH}/${item.callId}`}>{item.callId}</Link>,
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
    header: 'Name',
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
      { id: 'initiationTimeStamp', label: 'Initiation Timestamp' },
      { id: 'recordingStatus', label: 'Status' },
      { id: 'summary', label: 'Summary' },
      { id: 'conversationDuration', label: 'Duration' },
    ],
  },
];

const VISIBLE_CONTENT = ['agentId', 'initiationTimeStamp', 'recordingStatus', 'summary', 'conversationDuration'];

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

// number of shards per day used by the list calls API
export const CALL_LIST_SHARDS_PER_DAY = 6;
const TIME_PERIOD_DROPDOWN_CONFIG = {
  'refresh-2h': { count: 0.5, text: '2 hrs' },
  'refresh-4h': { count: 1, text: '4 hrs' },
  'refresh-8h': { count: CALL_LIST_SHARDS_PER_DAY / 3, text: '8 hrs' },
  'refresh-1d': { count: CALL_LIST_SHARDS_PER_DAY, text: '1 day' },
  'refresh-2d': { count: 2 * CALL_LIST_SHARDS_PER_DAY, text: '2 days' },
  'refresh-1w': { count: 7 * CALL_LIST_SHARDS_PER_DAY, text: '1 week' },
  'refresh-2w': { count: 14 * CALL_LIST_SHARDS_PER_DAY, text: '2 weeks' },
  'refresh-1m': { count: 30 * CALL_LIST_SHARDS_PER_DAY, text: '30 days' },
};
const TIME_PERIOD_DROPDOWN_ITEMS = Object.keys(TIME_PERIOD_DROPDOWN_CONFIG).map((k) => ({
  id: k,
  ...TIME_PERIOD_DROPDOWN_CONFIG[k],
}));

// local storage key to persist the last periods to load
export const PERIODS_TO_LOAD_STORAGE_KEY = 'periodsToLoad';

export const CallsCommonHeader = ({ resourceName = 'Meetings', ...props }) => {
  const onPeriodToLoadChange = ({ detail }) => {
    const { id } = detail;
    const shardCount = TIME_PERIOD_DROPDOWN_CONFIG[id].count;
    props.setPeriodsToLoad(shardCount);
    localStorage.setItem(PERIODS_TO_LOAD_STORAGE_KEY, JSON.stringify(shardCount));
  };

  // eslint-disable-next-line
  const periodText =
    TIME_PERIOD_DROPDOWN_ITEMS.filter((i) => i.count === props.periodsToLoad)[0]?.text || '';

  return (
    <TableHeader
      title={resourceName}
      actionButtons={
        <SpaceBetween size="xxs" direction="horizontal">
          <ButtonDropdown loading={props.loading} onItemClick={onPeriodToLoadChange} items={TIME_PERIOD_DROPDOWN_ITEMS}>
            {`Load: ${periodText}`}
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
        </SpaceBetween>
      }
      {...props}
    />
  );
};
