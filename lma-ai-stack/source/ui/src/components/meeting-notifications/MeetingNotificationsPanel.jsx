/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * A meeting's notifications as a feed in time order: guidance for the people
 * running the meeting, and any other category a feature publishes. Shown in the
 * meeting page's Notifications drawer, and inline on an embedded meeting page.
 *
 * Only the meeting's owner, the people it is shared with and Admins receive
 * them; they are never posted to the meeting's chat. Dismissing one and muting
 * a category change only the signed-in user's view.
 */
import React, { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import ReactMarkdown from 'react-markdown';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  Select,
  SpaceBetween,
  StatusIndicator,
  Toggle,
} from '@cloudscape-design/components';
import { notificationActions } from './notification-actions';
import './MeetingNotificationsPanel.css';

const logger = new ConsoleLogger('MeetingNotificationsPanel');

/** The StatusIndicator type and words each severity is shown with. */
export const SEVERITY_DISPLAY = {
  INFO: { type: 'info', label: 'Info' },
  SUGGESTION: { type: 'info', label: 'Suggestion' },
  WARNING: { type: 'warning', label: 'Warning' },
  URGENT: { type: 'error', label: 'Urgent' },
};

/** The heading of a notification published without a title. */
const defaultTitle = (category) => (category === 'guidance' ? 'Guidance for this meeting' : 'Notification');

/** A category or type name as words: "ask_question" reads "ask question". */
export const humanize = (name) => (name || '').replace(/[_-]+/g, ' ').trim();

const ALL = 'all';

// Markdown without raw HTML (react-markdown escapes it) and without pictures, so
// a notification never makes the browser fetch from an address its text names.
const markdownComponents = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

const NotificationCard = ({ notification, dismissed, onDismiss, onRestore, client }) => {
  const [status, setStatus] = useState(null);
  const severity = SEVERITY_DISPLAY[notification.Severity] || SEVERITY_DISPLAY.INFO;
  const actions = notificationActions(notification);
  const run = async (action) => {
    setStatus(null);
    try {
      if (action.kind === 'copy') {
        await navigator.clipboard.writeText(action.text);
        setStatus({ type: 'success', text: 'Copied' });
      } else {
        await action.run(client());
        setStatus({ type: 'success', text: 'Done' });
      }
    } catch (err) {
      logger.error('notification action failed', err);
      setStatus({ type: 'error', text: action.kind === 'copy' ? 'Could not copy' : 'Could not do that' });
    }
  };
  const updated = (notification.Revision || 1) > 1;
  return (
    <li
      className={`meeting-notification${dismissed ? ' meeting-notification-dismissed' : ''}`}
      data-testid="meeting-notification"
      data-notification-id={notification.NotificationId}
    >
      <SpaceBetween size="xxs">
        <div className="meeting-notification-meta">
          <StatusIndicator type={severity.type}>{severity.label}</StatusIndicator>
          <Box variant="small" color="text-body-secondary">
            {humanize(notification.Category)}
            {notification.Type ? ` · ${humanize(notification.Type)}` : ''}
            {notification.CreatedAt ? ` · ${new Date(notification.CreatedAt).toLocaleTimeString()}` : ''}
            {updated ? ' · updated' : ''}
          </Box>
        </div>
        <Box variant="h4">{notification.Title || defaultTitle(notification.Category)}</Box>
        {notification.Body && (
          <div className="meeting-notification-body">
            <ReactMarkdown disallowedElements={['img']} unwrapDisallowed components={markdownComponents}>
              {notification.Body}
            </ReactMarkdown>
          </div>
        )}
        <div className="meeting-notification-buttons">
          {actions.map((action) => (
            <Button key={`${action.kind}:${action.label}`} variant="normal" onClick={() => run(action)}>
              {action.label}
            </Button>
          ))}
          {dismissed ? (
            <Button variant="link" onClick={() => onRestore(notification.NotificationId)}>
              Restore
            </Button>
          ) : (
            <Button
              variant="icon"
              iconName="close"
              ariaLabel={`Dismiss ${notification.Title || defaultTitle(notification.Category)}`}
              onClick={() => onDismiss(notification.NotificationId)}
            />
          )}
          {status && <StatusIndicator type={status.type}>{status.text}</StatusIndicator>}
        </div>
        {notification.Source && (
          <Box variant="small" color="text-body-secondary">
            {notification.Source === 'mcp' && notification.CreatedBy
              ? `From ${notification.CreatedBy}, through an MCP client`
              : `From ${humanize(notification.Source)}`}
          </Box>
        )}
      </SpaceBetween>
    </li>
  );
};

NotificationCard.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  notification: PropTypes.object.isRequired,
  dismissed: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  onRestore: PropTypes.func.isRequired,
  client: PropTypes.func.isRequired,
};

/**
 * `notifications` is what useMeetingNotifications returns for the meeting.
 */
const MeetingNotificationsPanel = ({ notifications }) => {
  const {
    items,
    dismissed,
    mutedHere,
    mutedEverywhere,
    isMuted,
    loading,
    error,
    now,
    client,
    dismiss,
    restore,
    muteHere,
    unmuteHere,
    muteEverywhere,
    unmuteEverywhere,
  } = notifications;
  const [category, setCategory] = useState(ALL);
  const [showDismissed, setShowDismissed] = useState(false);

  const categories = useMemo(
    () => [...new Set([...items.map((item) => item.Category), ...mutedHere, ...mutedEverywhere])].sort(),
    [items, mutedHere, mutedEverywhere],
  );
  const shown = items.filter((item) => {
    if (category !== ALL && item.Category !== category) return false;
    if (isMuted(item.Category)) return false;
    if (item.ExpiresAt && Date.parse(item.ExpiresAt) <= now) return false;
    return showDismissed || !dismissed.includes(item.NotificationId);
  });
  const hiddenByMute = items.filter((item) => isMuted(item.Category)).length;

  const muteItems = categories.flatMap((name) => {
    const words = humanize(name);
    const here = mutedHere.includes(name);
    const everywhere = mutedEverywhere.includes(name);
    return [
      {
        id: `${here ? 'unmute-here' : 'mute-here'}:${name}`,
        text: here ? `Show ${words} on this meeting` : `Mute ${words} on this meeting`,
      },
      {
        id: `${everywhere ? 'unmute-everywhere' : 'mute-everywhere'}:${name}`,
        text: everywhere ? `Show ${words} on every meeting` : `Mute ${words} on every meeting`,
      },
    ];
  });
  const onMuteItem = ({ detail }) => {
    const [action, ...rest] = detail.id.split(':');
    const name = rest.join(':');
    ({
      'mute-here': muteHere,
      'unmute-here': unmuteHere,
      'mute-everywhere': muteEverywhere,
      'unmute-everywhere': unmuteEverywhere,
    })[action]?.(name);
  };

  const options = [
    { value: ALL, label: 'All categories' },
    ...categories.map((name) => ({ value: name, label: humanize(name) })),
  ];

  return (
    <div className="meeting-notifications" data-testid="meeting-notifications">
      <SpaceBetween size="s">
        <Box variant="small" color="text-body-secondary">
          Shown only in LMA, to the people who can see this meeting. Never posted to the meeting chat.
        </Box>
        <div className="meeting-notifications-controls">
          <Select
            selectedOption={options.find((option) => option.value === category) || options[0]}
            options={options}
            onChange={({ detail }) => setCategory(detail.selectedOption.value)}
            ariaLabel="Category"
          />
          <Toggle checked={showDismissed} onChange={({ detail }) => setShowDismissed(detail.checked)}>
            Show dismissed
          </Toggle>
          {categories.length > 0 && (
            <ButtonDropdown items={muteItems} onItemClick={onMuteItem} variant="normal">
              Mute
            </ButtonDropdown>
          )}
        </div>
        {error && <Alert type="error">{error}</Alert>}
        {loading && items.length === 0 && <StatusIndicator type="loading">Loading notifications</StatusIndicator>}
        {!loading && shown.length === 0 && (
          <Box color="text-body-secondary">
            {items.length === 0 ? 'No notifications for this meeting yet.' : 'Nothing to show with these settings.'}
          </Box>
        )}
        {hiddenByMute > 0 && (
          <Box variant="small" color="text-body-secondary">
            {hiddenByMute === 1 ? '1 notification is' : `${hiddenByMute} notifications are`} hidden by a muted category.
          </Box>
        )}
        <ul className="meeting-notification-list">
          {shown.map((notification) => (
            <NotificationCard
              key={notification.NotificationId}
              notification={notification}
              dismissed={dismissed.includes(notification.NotificationId)}
              onDismiss={dismiss}
              onRestore={restore}
              client={client}
            />
          ))}
        </ul>
      </SpaceBetween>
    </div>
  );
};

MeetingNotificationsPanel.propTypes = {
  // What useMeetingNotifications returns.
  // eslint-disable-next-line react/forbid-prop-types
  notifications: PropTypes.object.isRequired,
};

export default MeetingNotificationsPanel;
