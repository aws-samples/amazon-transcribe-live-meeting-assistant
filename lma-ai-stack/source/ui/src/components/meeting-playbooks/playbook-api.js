/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting playbooks a page can offer: meetingPlaybookOptions, and a hook
 * that reads it. Applying one goes through the meeting page's own writer
 * (sendUpdateMeetingPreparation, kind "playbook"), so the meeting updates as
 * every other change to it does. See docs/meeting-playbooks.md.
 */
import { useEffect, useState } from 'react';
import { generateClient } from 'aws-amplify/api';

export const MEETING_PLAYBOOK_OPTIONS = /* GraphQL */ `
  query MeetingPlaybookOptions($CallId: ID, $Title: String, $Agenda: String, $Invitation: String) {
    meetingPlaybookOptions(CallId: $CallId, Title: $Title, Agenda: $Agenda, Invitation: $Invitation)
  }
`;

const EMPTY = { Playbooks: [], Suggestions: [], ProfileDefault: null, Current: null, Deployment: {} };

/** The AWSJSON answer as an object, whether it arrived as text or already parsed. */
export const parseOptions = (value) => {
  if (!value) return EMPTY;
  if (typeof value === 'object') return { ...EMPTY, ...value };
  try {
    return { ...EMPTY, ...JSON.parse(value) };
  } catch {
    return EMPTY;
  }
};

let sharedClient;
const clientOf = (client) => {
  if (client) return client;
  if (!sharedClient) sharedClient = generateClient();
  return sharedClient;
};

export const fetchPlaybookOptions = async ({ callId, title, agenda, invitation, client } = {}) => {
  const response = await clientOf(client).graphql({
    query: MEETING_PLAYBOOK_OPTIONS,
    variables: {
      ...(callId ? { CallId: callId } : {}),
      ...(title ? { Title: title } : {}),
      ...(agenda ? { Agenda: agenda } : {}),
      ...(invitation ? { Invitation: invitation } : {}),
    },
  });
  return parseOptions(response?.data?.meetingPlaybookOptions);
};

/**
 * The options for a meeting or a draft, read again when what they depend on
 * changes. `enabled` false reads nothing (a Participant, or a page that has no
 * control to show). The title is read after a pause, so typing a meeting name
 * does not send a request a keystroke.
 */
export const usePlaybookOptions = ({ callId, title, agenda, enabled = true, refreshKey, client } = {}) => {
  const [state, setState] = useState({ loading: Boolean(enabled), error: '', options: EMPTY });
  useEffect(() => {
    if (!enabled) {
      setState({ loading: false, error: '', options: EMPTY });
      return undefined;
    }
    let live = true;
    const timer = setTimeout(async () => {
      setState((current) => ({ ...current, loading: true, error: '' }));
      try {
        const options = await fetchPlaybookOptions({ callId, title, agenda, client });
        if (live) setState({ loading: false, error: '', options });
      } catch (error) {
        const message = error?.errors?.[0]?.message || error?.message || 'The playbooks could not be read.';
        if (live) setState({ loading: false, error: message, options: EMPTY });
      }
    }, 400);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [callId, title, agenda, enabled, refreshKey, client]);
  return state;
};
