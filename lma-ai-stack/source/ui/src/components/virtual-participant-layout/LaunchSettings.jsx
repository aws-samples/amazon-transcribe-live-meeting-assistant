/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, ExpandableSection, KeyValuePairs, SpaceBetween } from '@cloudscape-design/components';

import { platformLabel } from '../common/meeting-header';
import { describeAgendaSource, parseAgendaItems } from '../common/agenda';
import {
  DEPLOYMENT_DEFAULT,
  describeConsent,
  describeMeetingPassword,
  describeStart,
  describeToggle,
  describeZoomAccount,
  meetingIdLabel,
} from './launch-settings';

const NOT_SET = 'Not set';

const AgendaValue = ({ agenda = null, agendaSource = null }) => {
  const items = parseAgendaItems(agenda);
  if (items.length === 0) return NOT_SET;
  const source = describeAgendaSource(agendaSource);
  return (
    <ExpandableSection headerText={`${items.length} item${items.length === 1 ? '' : 's'}`} variant="footer">
      <SpaceBetween size="xs">
        <ol style={{ margin: 0, paddingInlineStart: '1.5em' }}>
          {items.map((item, index) => (
            // Agenda items can repeat, so the position is part of the key.
            // eslint-disable-next-line react/no-array-index-key
            <li key={`${index}-${item}`}>{item}</li>
          ))}
        </ol>
        {source && <Box color="text-body-secondary">{source}</Box>}
      </SpaceBetween>
    </ExpandableSection>
  );
};

AgendaValue.propTypes = {
  agenda: PropTypes.string,
  agendaSource: PropTypes.string,
};

const StartValue = ({ vp }) => {
  const { primary, secondary } = describeStart(vp);
  return (
    <SpaceBetween size="xxxs">
      <span>{primary}</span>
      {secondary && <Box color="text-body-secondary">{secondary}</Box>}
    </SpaceBetween>
  );
};

StartValue.propTypes = {
  vp: PropTypes.shape({}).isRequired,
};

/**
 * The settings the Virtual Participant was created with, as the Create Virtual
 * Participant form asked for them. The meeting password is never shown: the
 * page reads only whether one was given.
 */
const LaunchSettings = ({ vp, headerActions = null }) => {
  const zoomAccount = describeZoomAccount(vp);
  const meetingItems = [
    { label: 'Meeting name', value: vp.meetingName || NOT_SET },
    { label: 'Platform', value: platformLabel(vp.meetingPlatform) || NOT_SET },
    {
      label: meetingIdLabel(vp.meetingPlatform, vp.meetingId),
      value: <span style={{ overflowWrap: 'anywhere' }}>{vp.meetingId || NOT_SET}</span>,
    },
    { label: 'Meeting password', value: describeMeetingPassword(vp) },
    ...(zoomAccount ? [{ label: 'Zoom account', value: zoomAccount }] : []),
    { label: 'Meeting time', value: <StartValue vp={vp} /> },
    { label: 'Agenda', value: <AgendaValue agenda={vp.agenda} agendaSource={vp.agendaSource} /> },
  ];

  const captureItems = [
    { label: 'Video (screen) recording', value: describeToggle(vp.enableVideoRecording) },
    {
      label: 'Audio recording and transcript',
      value: 'Not a per-meeting choice (deployment setting)',
    },
  ];

  const consentItems = [
    {
      label: 'Meeting consent',
      value: describeConsent(vp),
      info:
        describeConsent(vp) === DEPLOYMENT_DEFAULT ? undefined : (
          <Box variant="small" color="text-body-secondary">
            The deployment may not allow a less strict choice than its own, in which case its own applies.
          </Box>
        ),
    },
  ];

  return (
    <ExpandableSection
      variant="container"
      headerText="Launch settings"
      headerDescription="What was asked for when this Virtual Participant was created"
      headerActions={headerActions}
      defaultExpanded={false}
    >
      <KeyValuePairs
        columns={3}
        items={[
          { type: 'group', title: 'Meeting', items: meetingItems },
          { type: 'group', title: 'Capture', items: captureItems },
          { type: 'group', title: 'Consent', items: consentItems },
        ]}
      />
    </ExpandableSection>
  );
};

LaunchSettings.propTypes = {
  vp: PropTypes.shape({
    meetingName: PropTypes.string,
    meetingPlatform: PropTypes.string,
    meetingId: PropTypes.string,
    hasMeetingPassword: PropTypes.bool,
    useStoredZoomAccount: PropTypes.bool,
    isScheduled: PropTypes.bool,
    meetingTime: PropTypes.number,
    scheduledFor: PropTypes.string,
    scheduledTimeZone: PropTypes.string,
    agenda: PropTypes.string,
    agendaSource: PropTypes.string,
    enableVideoRecording: PropTypes.bool,
    consentMode: PropTypes.string,
    consentRule: PropTypes.string,
  }).isRequired,
  headerActions: PropTypes.node,
};

export default LaunchSettings;
