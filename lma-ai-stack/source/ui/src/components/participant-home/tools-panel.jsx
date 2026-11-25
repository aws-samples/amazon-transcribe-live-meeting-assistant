/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The help panel of each Participant page, opened from the (i) button at the
 * right of the page and the Info links on it, as on every other page.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { HelpPanel, Icon } from '@cloudscape-design/components';

import { MY_CONSENTS_PATH, SHARED_MEETINGS_PATH, VOICE_ENROLLMENT_PATH } from '../../routes/constants';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const DocsLink = ({ page, children }) => (
  <li>
    <a href={`${DOCS_BASE}/${page}`} target="_blank" rel="noopener noreferrer">
      <Icon name="external" /> {children}
    </a>
  </li>
);

DocsLink.propTypes = { page: PropTypes.string.isRequired, children: PropTypes.node.isRequired };

const HELP = {
  home: {
    header: 'Live Meeting Assistant',
    body: (
      <p>
        Your account is a Participant account. You can read the meetings their owners share with you, ask the meeting
        assistant about them, give or withdraw consent before a meeting, and enroll your voice.
      </p>
    ),
  },
  sharedMeetings: {
    header: 'Shared meetings',
    body: (
      <p>
        The meetings their owners have shared with you, newest first. Open one to read its transcript, summary,
        recordings and consent record. A meeting the owner stops sharing leaves this list.
      </p>
    ),
  },
  meeting: {
    header: 'Shared meeting',
    body: (
      <>
        <p>
          A meeting shared with you, read-only. The Transcript tab follows a live meeting as it is transcribed (turn
          Auto-scroll off to stay where you are), and plays the recording once the meeting has ended. The Summary tab
          has the summary, action items, chapters and agenda; the Details tab has the participant list and the consent
          record.
        </p>
        <p>
          The Assistant button opens the meeting assistant beside the tabs. Your questions and its answers are seen by
          you only, and it answers from this meeting with the tools the deployment offers for Participant use.
        </p>
      </>
    ),
  },
  consents: {
    header: 'My consents',
    body: (
      <p>
        Give consent before a meeting to its being recorded and transcribed, from the consent link its organizer sends
        you, and withdraw it at any time.
      </p>
    ),
  },
  voice: {
    header: 'Voice enrollment',
    body: (
      <p>
        Enroll your own voice so the meetings you take part in can name you as their speaker, and delete the enrollment
        again.
      </p>
    ),
  },
};

/** Which page's help a path shows. */
export const helpFor = (pathname) => {
  if (pathname.startsWith(`${SHARED_MEETINGS_PATH}/`)) return HELP.meeting;
  if (pathname === SHARED_MEETINGS_PATH) return HELP.sharedMeetings;
  if (pathname === MY_CONSENTS_PATH || pathname.startsWith(`${MY_CONSENTS_PATH}/`)) return HELP.consents;
  if (pathname === VOICE_ENROLLMENT_PATH || pathname.startsWith(`${VOICE_ENROLLMENT_PATH}/`)) return HELP.voice;
  return HELP.home;
};

const ParticipantToolsPanel = ({ pathname }) => {
  const help = helpFor(pathname);
  return (
    <HelpPanel header={<h2>{help.header}</h2>}>
      {help.body}
      <h3>Documentation</h3>
      <ul>
        <DocsLink page="user-based-access-control/#participant">Participant accounts</DocsLink>
        <DocsLink page="web-ui-guide/">Web UI Guide</DocsLink>
        <DocsLink page="meeting-assistant/#participant-use">Meeting Assistant</DocsLink>
      </ul>
    </HelpPanel>
  );
};

ParticipantToolsPanel.propTypes = { pathname: PropTypes.string.isRequired };

export default ParticipantToolsPanel;
