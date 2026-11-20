/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useState } from 'react';
import { Input, Pagination, Select, SpaceBetween, Table, TextFilter } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import useCallsContext from '../../contexts/calls';

import mapCallsAttributes from '../common/map-call-attributes';
import { paginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import { exportToExcel } from '../common/download-func';
import { exportableMeeting, filterMeetingsByPreparation } from '../common/meeting-preparation';

import {
  CallsPreferences,
  CallsCommonHeader,
  COLUMN_DEFINITIONS_MAIN,
  KEY_COLUMN_ID,
  SELECTION_LABELS,
  DEFAULT_PREFERENCES,
  DEFAULT_SORT_COLUMN,
  withKnownColumns,
} from './calls-table-config';

import { getFilterCounterText, TableEmptyState, TableNoMatchState } from '../common/table';

import '@cloudscape-design/global-styles/index.css';

const logger = new ConsoleLogger('CallList');

/** The audience filter's choices; see filterMeetingsByPreparation. */
export const AUDIENCE_FILTER_OPTIONS = [
  { value: '', label: 'Any audience' },
  { value: 'internal', label: 'Internal' },
  { value: 'external', label: 'External' },
  { value: 'unspecified', label: 'Unspecified' },
];

const CallList = () => {
  const [callList, setCallList] = useState([]);
  // The label (key=value, or key alone) and audience filters, applied before
  // the text filter.
  const [labelFilter, setLabelFilter] = useState('');
  const [audienceFilter, setAudienceFilter] = useState(AUDIENCE_FILTER_OPTIONS[0]);

  const {
    calls,
    isCallsListLoading,
    setIsCallsListLoading,
    setDateRange,
    setSelectedItems,
    setToolsOpen,
    dateRange,
    getCallDetailsFromCallIds,
    totalCallCount,
    totalCallCountTruncated,
  } = useCallsContext();

  const [savedPreferences, setPreferences] = useLocalStorage('call-list-preferences', DEFAULT_PREFERENCES);
  const preferences = withKnownColumns(savedPreferences);

  // prettier-ignore
  const {
    items, actions, filteredItemsCount, collectionProps, filterProps, paginationProps,
  } = useCollection(filterMeetingsByPreparation(callList, { label: labelFilter, audience: audienceFilter.value }), {
    filtering: {
      empty: <TableEmptyState resourceName="Meeting" />,
      noMatch: <TableNoMatchState onClearFilter={() => actions.setFiltering('')} />,
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: { defaultState: { sortingColumn: DEFAULT_SORT_COLUMN, isDescending: true } },
    selection: {
      keepSelection: false,
      trackBy: KEY_COLUMN_ID,
    },
  });

  useEffect(() => {
    if (!isCallsListLoading) {
      logger.debug('setting meeting list', calls);
      setCallList(mapCallsAttributes(calls));
    } else {
      logger.debug('meeting list is loading');
    }
  }, [isCallsListLoading, calls]);

  useEffect(() => {
    logger.debug('setting selected items', collectionProps.selectedItems);
    setSelectedItems(collectionProps.selectedItems);
  }, [collectionProps.selectedItems]);

  /* eslint-disable react/jsx-props-no-spreading */
  return (
    <Table
      {...collectionProps}
      header={
        <CallsCommonHeader
          resourceName="Meetings"
          calls={calls}
          selectedItems={collectionProps.selectedItems}
          totalItems={callList}
          updateTools={() => setToolsOpen(true)}
          loading={isCallsListLoading}
          setIsLoading={setIsCallsListLoading}
          dateRange={dateRange}
          setDateRange={setDateRange}
          getCallDetailsFromCallIds={getCallDetailsFromCallIds}
          totalCallCount={totalCallCount}
          totalCallCountTruncated={totalCallCountTruncated}
          loadedCallCount={calls.length}
          downloadToExcel={() => exportToExcel(callList.map(exportableMeeting), 'Meeting-List')}
          // eslint-disable-next-line max-len, prettier/prettier
        />
      }
      columnDefinitions={COLUMN_DEFINITIONS_MAIN}
      items={items}
      loading={isCallsListLoading}
      loadingText="Loading meetings"
      selectionType="multi"
      ariaLabels={SELECTION_LABELS}
      filter={
        <SpaceBetween size="xs" direction="horizontal">
          <TextFilter
            {...filterProps}
            filteringAriaLabel="Filter meetings"
            filteringPlaceholder="Find meetings"
            countText={getFilterCounterText(filteredItemsCount)}
          />
          <Input
            type="search"
            value={labelFilter}
            onChange={({ detail }) => setLabelFilter(detail.value)}
            placeholder="Label, e.g. project=acme"
            ariaLabel="Filter meetings by label (key=value)"
          />
          <Select
            selectedOption={audienceFilter}
            options={AUDIENCE_FILTER_OPTIONS}
            onChange={({ detail }) => setAudienceFilter(detail.selectedOption)}
            ariaLabel="Filter meetings by audience"
          />
        </SpaceBetween>
      }
      wrapLines={preferences.wrapLines}
      pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
      preferences={<CallsPreferences preferences={preferences} setPreferences={setPreferences} />}
      trackBy={items.callId}
      visibleColumns={[KEY_COLUMN_ID, ...preferences.visibleContent]}
      resizableColumns
    />
  );
};

export default CallList;
