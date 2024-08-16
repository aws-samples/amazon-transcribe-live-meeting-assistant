// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Switch, Route, useRouteMatch } from 'react-router-dom';
import { AppLayout, Flashbar } from '@awsui/components-react';

import { Logger } from 'aws-amplify';

import { CallsContext } from '../../contexts/calls';

import useNotifications from '../../hooks/use-notifications';
import useSplitPanel from '../../hooks/use-split-panel';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';

import CallList from '../call-list';
import CallDetails from '../call-details';
import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';
import SplitPanel from './calls-split-panel';

import { CALL_LIST_SHARDS_PER_DAY, PERIODS_TO_LOAD_STORAGE_KEY } from '../call-list/calls-table-config';

import useAppContext from '../../contexts/app';

const logger = new Logger('CallAnalyticsLayout');

const CallAnalyticsLayout = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();

  const { path } = useRouteMatch();
  logger.debug('path', path);

  const notifications = useNotifications();
  const [toolsOpen, setToolsOpen] = useState(false);
  const [selectedItems, setSelectedItems] = useState([]);

  const getInitialPeriodsToLoad = () => {
    // default to 2 hours - half of one (4hr) shard period
    let periods = 0.5;
    try {
      const periodsFromStorage = Math.abs(JSON.parse(localStorage.getItem(PERIODS_TO_LOAD_STORAGE_KEY)));
      // prettier-ignore
      if (
        !Number.isSafeInteger(periodsFromStorage)
        // load max of to 30 days
        || periodsFromStorage > CALL_LIST_SHARDS_PER_DAY * 30
      ) {
        logger.warn('invalid initialPeriodsToLoad value from local storage');
      } else {
        periods = (periodsFromStorage > 0) ? periodsFromStorage : periods;
        localStorage.setItem(PERIODS_TO_LOAD_STORAGE_KEY, JSON.stringify(periods));
      }
    } catch {
      logger.warn('failed to parse initialPeriodsToLoad from local storage');
    }

    return periods;
  };
  const initialPeriodsToLoad = getInitialPeriodsToLoad();

  const {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    periodsToLoad,
    setLiveTranscriptCallId,
    setIsCallsListLoading,
    setPeriodsToLoad,
    sendGetTranscriptSegmentsRequest,
  } = useCallsGraphQlApi({ initialPeriodsToLoad });

  // eslint-disable-next-line prettier/prettier
  const {
    splitPanelOpen,
    onSplitPanelToggle,
    splitPanelSize,
    onSplitPanelResize,
  } = useSplitPanel(selectedItems);

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const callsContextValue = {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    selectedItems,
    sendGetTranscriptSegmentsRequest,
    setIsCallsListLoading,
    setLiveTranscriptCallId,
    setPeriodsToLoad,
    setToolsOpen,
    setSelectedItems,
    periodsToLoad,
    toolsOpen,
  };

  return (
    <CallsContext.Provider value={callsContextValue}>
      <AppLayout
        headerSelector="#top-navigation"
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<Breadcrumbs />}
        notifications={<Flashbar items={notifications} />}
        tools={<ToolsPanel />}
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        splitPanelOpen={splitPanelOpen}
        onSplitPanelToggle={onSplitPanelToggle}
        splitPanelSize={splitPanelSize}
        onSplitPanelResize={onSplitPanelResize}
        splitPanel={<SplitPanel />}
        content={
          <Switch>
            <Route exact path={path}>
              <CallList />
            </Route>
            <Route path={`${path}/:callId`}>
              <CallDetails />
            </Route>
          </Switch>
        }
        ariaLabels={appLayoutLabels}
      />
    </CallsContext.Provider>
  );
};

export default CallAnalyticsLayout;
