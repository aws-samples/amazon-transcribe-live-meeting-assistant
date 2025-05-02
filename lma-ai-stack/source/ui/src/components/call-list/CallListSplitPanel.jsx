// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect } from 'react';
import { SplitPanel } from '@awsui/components-react';
import { Logger } from 'aws-amplify';

import useCallsContext from '../../contexts/calls';

import { getPanelContent, SPLIT_PANEL_I18NSTRINGS } from './calls-split-panel-config';
import { IN_PROGRESS_STATUS } from '../common/get-recording-status';

import '@awsui/global-styles/index.css';

const logger = new Logger('CallListSplitPanel');

const CallListSplitPanel = () => {
  const {
    callTranscriptPerCallId,
    setLiveTranscriptCallId,
    sendGetTranscriptSegmentsRequest,
    selectedItems,
    setToolsOpen,
    getCallDetailsFromCallIds,
  } = useCallsContext();

  const { header: panelHeader, body: panelBody } = getPanelContent(
    selectedItems,
    'multiple',
    setToolsOpen,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
  );

  const sendTranscriptSegmentsRequests = async (item) => {
    const { callId } = item;
    if (!callTranscriptPerCallId[callId]) {
      await sendGetTranscriptSegmentsRequest(callId);
    }
    if (item?.recordingStatusLabel === IN_PROGRESS_STATUS) {
      setLiveTranscriptCallId(callId);
    }
  };

  useEffect(() => {
    logger.debug('selected items', selectedItems);

    if (selectedItems?.length === 1) {
      const item = selectedItems[0];
      sendTranscriptSegmentsRequests(item);
    }

    return () => {
      logger.debug('set live transcript contact to null');
      setLiveTranscriptCallId(null);
    };
  }, [selectedItems]);

  return (
    <SplitPanel header={panelHeader} i18nStrings={SPLIT_PANEL_I18NSTRINGS}>
      {panelBody}
    </SplitPanel>
  );
};

export default CallListSplitPanel;
