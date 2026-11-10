/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting assistant, as a panel beside the meeting page's tabs.
 *
 * CallPanel decides whether it is open (hooks/use-assistant-panel.js) and lays
 * it out (CallPanel.css: a column beside the tabs, or over them where the page
 * is narrow). This component is the panel itself: its header, with the
 * administrator's settings menu and a close button, and the chat frame
 * (public/strands-chat.html), whose requests CallPanel answers through
 * useStrandsChatBridge and useChatKeyframeLinks.
 *
 * It is created the first time the panel opens and is hidden, not removed,
 * when it closes, so the frame keeps its conversation across tab changes and
 * reopening. Besides the bridge's messages, the frame and this panel exchange
 * a few of their own, all on the page's origin and only with this frame:
 * - to the frame: STRANDS_HOST_FEATURES (what this page can do with a frame of
 *   the shared screen) once it has loaded, and STRANDS_FOCUS_INPUT when the
 *   panel is opened, which puts the keyboard focus in the composer;
 * - from the frame: STRANDS_ESCAPE (Escape pressed in the frame), which closes
 *   the panel, and STRANDS_SHOW_IN_TRANSCRIPT with a FrameId, from the "Show in
 *   transcript" button under a frame the assistant showed.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  FormField,
  Header,
  Icon,
  Input,
  Link,
  Modal,
  SpaceBetween,
  Textarea,
} from '@cloudscape-design/components';

import useUserGroups from '../../hooks/use-user-groups';
import { AccessDenied } from '../common/AdminOnly';
import { isConflict } from '../common/config-conflict';
import MCPServersModal from '../mcp-servers';

const client = generateClient();
const logger = new ConsoleLogger('MeetingAssistantPanel');

/** The id of the panel's region, for the header button's aria-controls. */
export const ASSISTANT_PANEL_ID = 'meeting-assistant-panel';

// Shown when the stored profile moved on while the Edit Chat Buttons dialog was
// open -- another administrator saved it, from this dialog or a configuration
// page, since they share one profile. The save is refused rather than replacing
// their edit, so the way forward is to reopen the dialog and reapply.
const CHAT_BUTTON_CONFLICT_MESSAGE =
  'Someone else saved the configuration while this dialog was open. Close and reopen it to see ' +
  'their version, then apply your changes again.';

const MeetingAssistantPanel = ({
  callId,
  open,
  onClose,
  focusRequest = 0,
  onShowInTranscript = undefined,
  showVNCPreview,
  setShowVNCPreview,
  vpData = null,
  loadingVP = false,
}) => {
  const regionRef = useRef(null);
  const frameRef = useRef(null);
  const frameLoadedRef = useRef(false);
  const pendingFocusRef = useRef(false);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onShowInTranscriptRef = useRef(onShowInTranscript);
  onShowInTranscriptRef.current = onShowInTranscript;

  const postToFrame = (message) => {
    const frame = frameRef.current;
    if (!frame?.contentWindow) return;
    frame.contentWindow.postMessage(message, new URL(frame.src, window.location.href).origin);
  };

  const [showEditModal, setShowEditModal] = useState(false);
  const [showMCPModal, setShowMCPModal] = useState(false);
  const [buttonConfig, setButtonConfig] = useState(null);
  // The version of the stored profile the dialog loaded, sent back on save so a
  // save against a profile someone else has since saved is refused rather than
  // replacing their edit.
  const [buttonConfigVersion, setButtonConfigVersion] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [alertMessage, setAlertMessage] = useState(null);
  const [alertType, setAlertType] = useState('success');

  // Check if user is admin (Amplify v6 pattern). `isAdminLoading` distinguishes
  // "not yet known" from "not an admin": the session resolves asynchronously, so
  // acting on isAdmin before then would show an admin the denial on first render.
  const { isAdmin, isLoading: isAdminLoading } = useUserGroups();

  const loadButtonConfiguration = async () => {
    setIsLoading(true);
    setAlertMessage(null);

    try {
      const query = `
        query GetChatButtonConfig($defaultId: ID!, $customId: ID!) {
          default: getChatButtonConfig(ChatButtonConfigId: $defaultId) {
            ChatButtonConfigId
          }
          custom: getChatButtonConfig(ChatButtonConfigId: $customId) {
            ChatButtonConfigId
          }
        }
      `;

      const variables = {
        defaultId: 'DefaultChatButtonConfig',
        customId: 'CustomChatButtonConfig',
      };

      const result = await client.graphql({ query, variables });

      // Parse the JSON strings returned from the resolver
      const defaultData = result?.data?.default?.ChatButtonConfigId
        ? JSON.parse(result.data.default.ChatButtonConfigId)
        : {};
      const customData = result?.data?.custom?.ChatButtonConfigId
        ? JSON.parse(result.data.custom.ChatButtonConfigId)
        : {};

      // Filter to only include button fields (format: N#LABEL)
      const buttonPattern = /^\d+#/;
      const filterButtons = (config) => {
        const filtered = {};
        Object.keys(config).forEach((key) => {
          if (buttonPattern.test(key)) {
            filtered[key] = config[key];
          }
        });
        return filtered;
      };

      const defaultButtons = filterButtons(defaultData);
      const customButtons = filterButtons(customData);

      // If custom buttons exist, use those; otherwise use defaults
      const hasCustomButtons = Object.keys(customButtons).length > 0;
      setButtonConfig(hasCustomButtons ? customButtons : defaultButtons);
      setButtonConfigVersion(typeof customData.Version === 'number' ? customData.Version : null);
    } catch (error) {
      console.error('Failed to load button configuration:', error);
      setAlertType('error');
      setAlertMessage('Failed to load button configuration');
    } finally {
      setIsLoading(false);
    }
  };

  // Load button configuration when modal opens
  useEffect(() => {
    if (showEditModal && isAdmin) {
      loadButtonConfiguration();
    }
  }, [showEditModal, isAdmin]);

  const handleSave = async () => {
    setIsLoading(true);
    setAlertMessage(null);

    try {
      const mutation = `
        mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
          updateChatButtonConfig(input: $input) {
            ChatButtonConfigId
            Success
            Version
          }
        }
      `;

      const variables = {
        input: {
          ChatButtonConfigId: 'CustomChatButtonConfig',
          ButtonConfig: JSON.stringify(buttonConfig),
          ExpectedVersion: buttonConfigVersion,
        },
      };

      const saved = await client.graphql({ query: mutation, variables });
      // So a second save from the same dialog is checked against this one.
      setButtonConfigVersion(saved?.data?.updateChatButtonConfig?.Version ?? null);

      setAlertType('success');
      setAlertMessage('Button configuration saved successfully!');

      // The frame reloads its buttons.
      postToFrame({ type: 'STRANDS_RELOAD_BUTTONS' });

      // Close modal after short delay
      setTimeout(() => {
        setShowEditModal(false);
      }, 1500);
    } catch (error) {
      console.error('Failed to save button configuration:', error);
      setAlertType('error');
      setAlertMessage(isConflict(error) ? CHAT_BUTTON_CONFLICT_MESSAGE : 'Failed to save button configuration');
    } finally {
      setIsLoading(false);
    }
  };

  const handleReset = async () => {
    if (
      !window.confirm('Are you sure you want to reset to default buttons? This will delete all custom configurations.')
    ) {
      return;
    }

    setIsLoading(true);
    setAlertMessage(null);

    try {
      const mutation = `
        mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
          updateChatButtonConfig(input: $input) {
            ChatButtonConfigId
            Success
            Version
          }
        }
      `;

      const variables = {
        input: {
          ChatButtonConfigId: 'CustomChatButtonConfig',
          ButtonConfig: JSON.stringify({}),
          ExpectedVersion: buttonConfigVersion,
        },
      };

      const saved = await client.graphql({ query: mutation, variables });
      setButtonConfigVersion(saved?.data?.updateChatButtonConfig?.Version ?? null);

      setAlertType('success');
      setAlertMessage('Reset to defaults successfully!');

      // The frame reloads its buttons.
      postToFrame({ type: 'STRANDS_RELOAD_BUTTONS' });

      // Reload to show default config
      setTimeout(() => {
        loadButtonConfiguration();
      }, 1500);
    } catch (error) {
      console.error('Failed to reset button configuration:', error);
      setAlertType('error');
      setAlertMessage(isConflict(error) ? CHAT_BUTTON_CONFLICT_MESSAGE : 'Failed to reset to defaults');
    } finally {
      setIsLoading(false);
    }
  };

  const handleButtonChange = (key, field, value) => {
    if (field === 'label') {
      // Update the key when label changes
      const match = key.match(/^(\d+)#/);
      if (match) {
        const sequence = parseInt(match[1], 10);
        const newKey = `${sequence}#${value}`;
        const newConfig = { ...buttonConfig };
        const oldPrompt = newConfig[key];
        delete newConfig[key];
        newConfig[newKey] = oldPrompt;
        setButtonConfig(newConfig);
      }
    } else {
      // Update prompt
      setButtonConfig({
        ...buttonConfig,
        [key]: value,
      });
    }
  };

  const handleDeleteButton = (key) => {
    const newConfig = { ...buttonConfig };
    delete newConfig[key];

    // Re-sequence remaining buttons to keep numbering contiguous
    const sortedEntries = Object.entries(newConfig).sort((a, b) => {
      const getSeq = (k) => {
        const m = k.match(/^(\d+)#/);
        return m ? parseInt(m[1], 10) : 999;
      };
      return getSeq(a[0]) - getSeq(b[0]);
    });

    const resequenced = {};
    sortedEntries.forEach(([k, v], idx) => {
      const label = k.replace(/^\d+#/, '');
      resequenced[`${idx + 1}#${label}`] = v;
    });

    setButtonConfig(resequenced);
  };

  const handleAddButton = () => {
    // Find the next sequence number
    const existingSequences = Object.keys(buttonConfig || {}).map((k) => {
      const m = k.match(/^(\d+)#/);
      return m ? parseInt(m[1], 10) : 0;
    });
    const nextSequence = existingSequences.length > 0 ? Math.max(...existingSequences) + 1 : 1;
    const newKey = `${nextSequence}#NEW BUTTON`;
    setButtonConfig({
      ...buttonConfig,
      [newKey]: 'Enter prompt here',
    });
  };

  const renderButtonEditors = () => {
    if (!buttonConfig) return null;

    // Sort by sequence number
    const sortedEntries = Object.entries(buttonConfig).sort((a, b) => {
      const getSequence = (key) => {
        const match = key.match(/^(\d+)#/);
        return match ? parseInt(match[1], 10) : 999;
      };
      return getSequence(a[0]) - getSequence(b[0]);
    });

    return sortedEntries.map(([key, prompt]) => {
      const match = key.match(/^(\d+)#(.*)$/);
      if (!match) return null;

      const [, sequence, label] = match;

      return (
        <Box key={`button-${sequence}`} padding={{ vertical: 's' }}>
          <SpaceBetween size="s">
            <SpaceBetween direction="horizontal" size="xs">
              <Box variant="h4">Button {sequence}</Box>
              <Button
                variant="icon"
                iconName="remove"
                onClick={() => handleDeleteButton(key)}
                ariaLabel={`Delete button ${sequence}`}
              />
            </SpaceBetween>
            <FormField label="Label">
              <Input value={label} onChange={(e) => handleButtonChange(key, 'label', e.detail.value)} />
            </FormField>
            <FormField label='Prompt (set to "NONE" to hide button)'>
              <Textarea value={prompt} onChange={(e) => handleButtonChange(key, 'prompt', e.detail.value)} rows={3} />
            </FormField>
          </SpaceBetween>
        </Box>
      );
    });
  };

  // The keyboard focus goes into the composer when the panel is opened from
  // the page (not when it opens by itself on a live meeting). On the first
  // opening the frame is still loading, so the request is repeated once it
  // has loaded.
  useEffect(() => {
    if (!focusRequest || !open) return;
    frameRef.current?.focus();
    postToFrame({ type: 'STRANDS_FOCUS_INPUT' });
    if (!frameLoadedRef.current) pendingFocusRef.current = true;
  }, [focusRequest]);

  const onFrameLoad = () => {
    frameLoadedRef.current = true;
    postToFrame({ type: 'STRANDS_HOST_FEATURES', showInTranscript: Boolean(onShowInTranscriptRef.current) });
    if (pendingFocusRef.current) {
      pendingFocusRef.current = false;
      postToFrame({ type: 'STRANDS_FOCUS_INPUT' });
    }
  };

  // Messages from this panel's frame, and from no other window.
  useEffect(() => {
    const onMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
      const type = event.data?.type;
      if (type === 'STRANDS_ESCAPE') {
        onCloseRef.current?.({ returnFocus: true });
      } else if (type === 'STRANDS_SHOW_IN_TRANSCRIPT' && typeof event.data.frameId === 'string') {
        onShowInTranscriptRef.current?.(event.data.frameId);
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  // Escape closes the panel while the focus is in it. A menu or dialog that
  // takes Escape for itself stops it first; the dialogs are rendered outside
  // the panel (React still delivers their keys here), so a key whose target is
  // not inside the panel's own elements is left alone.
  const onKeyDown = (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (!regionRef.current?.contains(event.target)) return;
    event.preventDefault();
    onClose({ returnFocus: true });
  };

  // Note: encodeURIComponent is required because callId may contain characters like '+'
  // which URLSearchParams.get() would otherwise decode as a space.
  const iframeSrc = `/strands-chat.html?callId=${encodeURIComponent(callId)}`;
  logger.debug(`Agent Assist Mode: ${import.meta.env.VITE_AGENT_ASSIST_MODE}, using frame ${iframeSrc}`);

  useEffect(() => {
    frameLoadedRef.current = false;
  }, [iframeSrc]);

  return (
    // The handler only listens for Escape bubbling up from the panel's own controls and frame.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <section
      ref={regionRef}
      id={ASSISTANT_PANEL_ID}
      className="meeting-assistant"
      aria-label="Meeting assistant"
      hidden={!open}
      onKeyDown={onKeyDown}
    >
      <div className="meeting-assistant-frame">
        <div className="meeting-assistant-header">
          <Header
            variant="h3"
            info={
              <Link variant="info" target="_blank" href="https://amazon.com/live-meeting-assistant">
                Info
              </Link>
            }
            actions={
              <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                {isAdmin && import.meta.env.VITE_AGENT_ASSIST_MODE === 'LAMBDA' ? (
                  <ButtonDropdown
                    items={[
                      { text: 'Edit Chat Buttons', id: 'edit-buttons' },
                      {
                        text: showVNCPreview ? 'Close VP Live View' : 'Open VP Live View',
                        id: 'live-view',
                        disabled: !vpData || !vpData.vncReady || loadingVP,
                      },
                      { text: 'MCP Servers', id: 'mcp-servers', disabled: false },
                    ]}
                    variant="icon"
                    onItemClick={(e) => {
                      if (e.detail.id === 'edit-buttons') {
                        setShowEditModal(true);
                      } else if (e.detail.id === 'live-view') {
                        setShowVNCPreview(!showVNCPreview);
                      } else if (e.detail.id === 'mcp-servers') {
                        setShowMCPModal(true);
                      }
                    }}
                    ariaLabel="Assistant settings"
                  >
                    <Icon name="settings" />
                  </ButtonDropdown>
                ) : null}
                <Button
                  variant="icon"
                  iconName="close"
                  ariaLabel="Close the assistant"
                  onClick={() => onClose({ returnFocus: true })}
                />
              </SpaceBetween>
            }
          >
            Meeting assistant
          </Header>
        </div>
        {/*
          The `sandbox` list is deliberately exactly these two tokens.

          What it buys: the frame renders agent output. The omitted tokens
          mean nothing in that output can submit a form, open a popup, raise
          a modal, or navigate the top-level page. Those are capabilities of
          the inert markup that reaches the DOM, not of script, so removing
          them is worth having on its own. This is the outer of three layers
          for the form case in particular -- renderMarkdownSafe in
          strands-chat.html strips form controls before they are rendered,
          and `form-action 'self'` in the distribution's CSP bounds where a
          submission could go -- and it is the layer that does not depend on
          the sanitizer having run.

          What it does not buy: `allow-scripts` plus `allow-same-origin` lets
          a script already running in the frame remove the sandbox attribute
          from its own frame element, so this is not a boundary against code
          executing inside the frame. It is a restriction on inert content.

          Why `allow-same-origin` stays: the parent/frame postMessage channel
          is pinned in both directions -- the parent only acts on messages
          whose `event.origin` equals its own, and addresses the frame with
          an explicit targetOrigin rather than '*'. Dropping the token would
          move the frame to an opaque origin and force both ends back to
          '*'/"null", trading a precise channel for a vaguer one.

          Cost: none. strands-chat.html has no form, no target="_blank", no
          window.open and no downloads of its own.
        */}
        <iframe
          ref={frameRef}
          className="meeting-assistant-chat"
          title="Meeting Assist"
          src={iframeSrc}
          sandbox="allow-scripts allow-same-origin"
          onLoad={onFrameLoad}
        />
      </div>

      {/*
        Edit Buttons Modal.

        The chat buttons are shared by everyone in the account, so saving them
        is an Admin operation, enforced on the AppSync field and in the
        resolver behind it.

        The !isAdmin branches below are a backstop, not a path a user takes
        today: the only thing that opens this dialog is the actions dropdown
        above, which is itself rendered only for an Admin, so a non-admin
        cannot reach it. They are here because an entry point added later
        without that gate is exactly the mistake worth surviving -- the dialog
        then explains itself instead of offering a form whose save the
        resolver refuses.
        Nothing user-facing should be documented as reachable through it.
      */}
      <Modal
        visible={showEditModal}
        onDismiss={() => setShowEditModal(false)}
        size="large"
        header="Edit Chat Buttons"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              {isAdmin && (
                <Button variant="normal" onClick={handleReset} disabled={isLoading}>
                  Reset to Defaults
                </Button>
              )}
              <Button variant="link" onClick={() => setShowEditModal(false)}>
                {isAdmin ? 'Cancel' : 'Close'}
              </Button>
              {isAdmin && (
                <Button variant="primary" onClick={handleSave} disabled={isLoading}>
                  Save Changes
                </Button>
              )}
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type={alertType} visible={!!alertMessage}>
            {alertMessage}
          </Alert>

          {!isAdminLoading && !isAdmin ? (
            <AccessDenied feature="the chat buttons" />
          ) : (
            <>
              <Box>
                Customize the suggestion buttons that appear in the chat. Changes will apply to all users. Set prompt to
                &quot;NONE&quot; to hide a button.
              </Box>

              {isLoading || isAdminLoading ? (
                <Box textAlign="center">Loading...</Box>
              ) : (
                <SpaceBetween size="m">
                  {renderButtonEditors()}
                  <Button variant="normal" iconName="add-plus" onClick={handleAddButton}>
                    Add Button
                  </Button>
                </SpaceBetween>
              )}
            </>
          )}
        </SpaceBetween>
      </Modal>

      {/* MCP Servers Modal */}
      <MCPServersModal visible={showMCPModal} onDismiss={() => setShowMCPModal(false)} vpData={vpData} />
    </section>
  );
};

MeetingAssistantPanel.propTypes = {
  callId: PropTypes.string.isRequired,
  open: PropTypes.bool.isRequired,
  /** Called with `{ returnFocus }`: true when the focus should go back to the header's Assistant button. */
  onClose: PropTypes.func.isRequired,
  /** A new value each time the panel is opened from the page: the focus goes into the composer. */
  focusRequest: PropTypes.number,
  /** Called with a FrameId when the assistant's "Show in transcript" is chosen under a frame. */
  onShowInTranscript: PropTypes.func,
  showVNCPreview: PropTypes.bool.isRequired,
  setShowVNCPreview: PropTypes.func.isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  vpData: PropTypes.object,
  loadingVP: PropTypes.bool,
};

export default MeetingAssistantPanel;
