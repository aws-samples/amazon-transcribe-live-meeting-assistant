/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The buttons a meeting notification can carry, and what pressing one does.
 *
 * A COPY action copies its payload's text to the clipboard. A MUTATION action's
 * payload names an action from MUTATION_ACTIONS below, with its arguments; the
 * button runs that action's own GraphQL operation as the signed-in user, so the
 * operation's resolver decides whether they may. The payload is never sent as
 * an operation, and its arguments never choose the meeting: the meeting is the
 * notification's own CallId. A name that is not listed here shows no button.
 *
 * The publish resolver (appsync/notificationPublishMeeting.js) refuses a
 * MUTATION action whose name is not on its own list;
 * test_meeting_notifications_contract.py holds the two lists to each other.
 */

// BEGIN notificationMutationActions -- one entry per action, each with the
// argument names it takes, its operation and how its variables are built from
// the notification and the arguments. Empty: no action is offered yet.
export const MUTATION_ACTIONS = {};
// END notificationMutationActions

const parsePayload = (payload) => {
  try {
    const parsed = JSON.parse(payload || '');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch (err) {
    return null;
  }
};

/**
 * The action a button runs, or null when the page should show no button for it:
 * { label, kind: 'copy', text } or { label, kind: 'mutation', name, run(client) }.
 */
export const resolveAction = (notification, action) => {
  if (!action || typeof action.Label !== 'string' || !action.Label.trim()) return null;
  const payload = parsePayload(action.Payload);
  if (!payload) return null;
  if (action.Kind === 'COPY') {
    if (typeof payload.text !== 'string' || !payload.text) return null;
    return { label: action.Label, kind: 'copy', text: payload.text };
  }
  if (action.Kind !== 'MUTATION') return null;
  const { action: name } = payload;
  if (typeof name !== 'string' || !Object.prototype.hasOwnProperty.call(MUTATION_ACTIONS, name)) return null;
  const definition = MUTATION_ACTIONS[name];
  const args = payload.args && typeof payload.args === 'object' && !Array.isArray(payload.args) ? payload.args : {};
  if (!Object.keys(args).every((key) => definition.args.includes(key))) return null;
  return {
    label: action.Label,
    kind: 'mutation',
    name,
    run: (client) =>
      client.graphql({
        query: definition.query,
        variables: definition.variables({ callId: notification.CallId, args }),
      }),
  };
};

/** The buttons a notification shows: at most two, leaving out any this page does not know. */
export const notificationActions = (notification) =>
  (notification?.Actions || [])
    .slice(0, 2)
    .map((action) => resolveAction(notification, action))
    .filter(Boolean);
