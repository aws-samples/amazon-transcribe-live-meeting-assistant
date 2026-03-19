/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * ComponentSelector - Routes to the appropriate embedded component based on query params.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Alert } from '@awsui/components-react';

import EmbedStreamAudio from './EmbedStreamAudio';
import EmbedCallDetails from './EmbedCallDetails';
import EmbedVirtualParticipant from './EmbedVirtualParticipant';
import EmbedMeetingLoader from './EmbedMeetingLoader';

/**
 * Maps component name to the appropriate React component.
 */
const COMPONENT_MAP = {
  'stream-audio': EmbedStreamAudio,
  'call-details': EmbedCallDetails,
  transcript: EmbedCallDetails,
  summary: EmbedCallDetails,
  chat: EmbedCallDetails,
  'vp-details': EmbedVirtualParticipant,
  vnc: EmbedVirtualParticipant,
  'meeting-loader': EmbedMeetingLoader,
};

/**
 * For single-component views (transcript, summary, chat, vnc),
 * automatically set the 'show' param if not explicitly provided.
 */
const getEffectiveShow = (component, show) => {
  if (show && show.length > 0) return show;

  switch (component) {
    case 'transcript':
      return ['transcript'];
    case 'summary':
      return ['summary'];
    case 'chat':
      return ['chat'];
    case 'vnc':
      return ['vnc'];
    case 'call-details':
      return ['transcript', 'summary', 'chat'];
    case 'vp-details':
      return ['vnc', 'transcript', 'summary', 'chat'];
    default:
      return [];
  }
};

const ComponentSelector = ({ params, sendToParent }) => {
  const { component } = params;

  const Component = COMPONENT_MAP[component];

  if (!Component) {
    return (
      <Box padding="l">
        <Alert type="error" header="Unknown Component">
          <p>
            The component <strong>&quot;{component}&quot;</strong> is not recognized.
          </p>
          <p>Available components:</p>
          <ul>
            <li>
              <strong>stream-audio</strong> - Stream Audio meeting component
            </li>
            <li>
              <strong>call-details</strong> - Full call details (transcript + summary + chat)
            </li>
            <li>
              <strong>transcript</strong> - Live meeting transcript only
            </li>
            <li>
              <strong>summary</strong> - Meeting/transcript summary only
            </li>
            <li>
              <strong>chat</strong> - Meeting Assist Bot chat only
            </li>
            <li>
              <strong>vp-details</strong> - Virtual participant details with selectable panels
            </li>
            <li>
              <strong>vnc</strong> - VNC live view of virtual participant
            </li>
            <li>
              <strong>meeting-loader</strong> - Meeting starter/loader page
            </li>
          </ul>
        </Alert>
      </Box>
    );
  }

  const effectiveParams = {
    ...params,
    show: getEffectiveShow(component, params.show),
  };

  return <Component params={effectiveParams} sendToParent={sendToParent} />;
};

ComponentSelector.propTypes = {
  params: PropTypes.shape({
    component: PropTypes.string.isRequired,
    show: PropTypes.arrayOf(PropTypes.string),
    layout: PropTypes.string,
    callId: PropTypes.string,
    vpId: PropTypes.string,
    meetingTopic: PropTypes.string,
    participants: PropTypes.string,
    owner: PropTypes.string,
    autoStart: PropTypes.bool,
    authMode: PropTypes.string,
  }).isRequired,
  sendToParent: PropTypes.func.isRequired,
};

export default ComponentSelector;
