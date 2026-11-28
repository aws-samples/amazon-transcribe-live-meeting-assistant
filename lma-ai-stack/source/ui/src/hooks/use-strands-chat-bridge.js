/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Answers the meeting assistant chat frame (public/strands-chat.html). The frame
 * has no GraphQL client of its own: it asks the page that hosts it, by
 * postMessage, for the signed-in user's context, the chat buttons, a chat answer
 * (sendChatMessage) and the answer's streamed tokens (onAddChatToken), and the
 * page runs each request with its own signed-in client and posts the result
 * back. Every page that hosts the frame uses this hook: the meeting page
 * (CallPanel) and the embeddable chat panel (EmbedCallDetails).
 *
 * Only messages from the page's own origin are answered, and each reply goes to
 * the frame that asked.
 */
import { useEffect, useRef } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import useAppContext from '../contexts/app';
import useUserGroups from './use-user-groups';
import { PARTICIPANT } from '../components/common/roles';

const logger = new ConsoleLogger('useStrandsChatBridge');

export const CHAT_FRAME_SELECTOR = 'iframe[src*="strands-chat.html"]';

const GET_CHAT_BUTTON_CONFIG = `
  query GetChatButtonConfig($defaultId: ID!, $customId: ID!) {
    default: getChatButtonConfig(ChatButtonConfigId: $defaultId) {
      ChatButtonConfigId
    }
    custom: getChatButtonConfig(ChatButtonConfigId: $customId) {
      ChatButtonConfigId
    }
  }
`;

const UPDATE_CHAT_BUTTON_CONFIG = `
  mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
    updateChatButtonConfig(input: $input) {
      ChatButtonConfigId
      Success
    }
  }
`;

export const SEND_CHAT_MESSAGE = `
  mutation SendChatMessage($input: SendChatMessageInput!) {
    sendChatMessage(input: $input) {
      MessageId
      Status
      CallId
      Response
    }
  }
`;

export const ON_ADD_CHAT_TOKEN = `
  subscription OnAddChatToken($callId: ID!, $messageId: ID!) {
    onAddChatToken(CallId: $callId, MessageId: $messageId) {
      CallId
      MessageId
      Token
      IsComplete
      Sequence
      Timestamp
      ThinkingStep {
        Type
        Content
        ToolName
        ToolInput
        ToolResult
        Success
      }
    }
  }
`;

const errorText = (error, fallback) => error?.message || error?.errors?.[0]?.message || fallback;

/** The chat buttons in force: the custom set when it has any button, else the defaults. */
export const mergeChatButtons = (result) => {
  const parse = (value) => (value ? JSON.parse(value) : {});
  const buttonPattern = /^\d+#/; // N#LABEL
  const filterButtons = (config) =>
    Object.fromEntries(Object.entries(config).filter(([key]) => buttonPattern.test(key)));
  const defaultButtons = filterButtons(parse(result?.data?.default?.ChatButtonConfigId));
  const customButtons = filterButtons(parse(result?.data?.custom?.ChatButtonConfigId));
  return Object.keys(customButtons).length > 0 ? { ...customButtons } : { ...defaultButtons };
};

/**
 * The chat buttons a Participant's frame shows, in the stored form ("N#LABEL":
 * prompt). A Participant's questions are answered with the Participant use
 * tools, which always include the current meeting's transcript and agenda and
 * nothing else unless an Admin offers more (docs/meeting-assistant.md#participant-use).
 * So these are the shipped buttons that need only those: the ones that ask
 * about past meetings, documents or the web, or ask the assistant to act in
 * the meeting, are left out. The deployment's own button set is read by Admins
 * and Users only, so a Participant always sees these.
 */
export const PARTICIPANT_CHAT_BUTTONS = Object.freeze({
  '1#SUMMARIZE': 'Summarize this meeting',
  '2#ACTIONS': 'What were the action items?',
  '3#TOPICS': 'What topics were discussed?',
  '4#AGENDA CHECK':
    'How are we doing against the agenda? For each agenda item, say whether it is covered, in progress or ' +
    'remaining, with the transcript times where it came up.',
  '5#CLARITY CHECK':
    'Which important points in this meeting so far are unclear, ambiguous or unresolved and should be revisited ' +
    'before the meeting ends? For each, say why it is unclear, with the transcript time where it came up.',
  '6#MISSING ACTION OWNER or ETA':
    'List the follow-up actions agreed in this meeting so far that have no clear owner or no due date. For each, ' +
    'say what is missing, with the transcript time where it came up.',
});

const userContextMessage = ({ user, userGroups, isAdmin }) => ({
  type: 'STRANDS_USER_CONTEXT',
  userGroups,
  isAdmin,
  email: user?.attributes?.email || user?.username || '',
});

/**
 * @param {string} callId the meeting the chat is about; the frame's first
 *   push of the user context is repeated when it changes.
 * @param {{ client?: object, onAnswerComplete?: Function, extraButtons?: object }} [options] the
 *   GraphQL client, for tests; a function called each time an answer has
 *   finished streaming (its last token has been passed to the frame), which
 *   the meeting page uses to mark the Assistant button while the panel is
 *   closed; and buttons the meeting adds before the deployment's, in the stored
 *   form ("N#LABEL": prompt) -- its playbook's quick actions
 *   (docs/meeting-playbooks.md). They are not shown to a Participant.
 */
const useStrandsChatBridge = (callId, { client, onAnswerComplete, extraButtons } = {}) => {
  const { user } = useAppContext();
  const { userGroups, isAdmin, role } = useUserGroups();

  // Read when a request arrives, so an answer carries the groups as they are
  // now rather than as they were when the listener was added.
  const contextRef = useRef({ user, userGroups, isAdmin, role });
  contextRef.current = { user, userGroups, isAdmin, role };
  const clientRef = useRef(client);
  if (!clientRef.current) clientRef.current = generateClient();
  const onAnswerCompleteRef = useRef(onAnswerComplete);
  onAnswerCompleteRef.current = onAnswerComplete;
  const extraButtonsRef = useRef(extraButtons || {});
  extraButtonsRef.current = extraButtons || {};
  const extraButtonsKey = JSON.stringify(extraButtons || {});

  // The meeting's own buttons changed (a playbook applied, switched or removed):
  // the frame reads its buttons again, so the change shows at once.
  const firstButtons = useRef(true);
  useEffect(() => {
    if (firstButtons.current) {
      firstButtons.current = false;
      return;
    }
    const iframe = document.querySelector(CHAT_FRAME_SELECTOR);
    if (iframe && iframe.contentWindow && iframe.src) {
      iframe.contentWindow.postMessage({ type: 'STRANDS_RELOAD_BUTTONS' }, new URL(iframe.src).origin);
    }
  }, [extraButtonsKey]);

  // Send the user context to the frame once it has had time to load.
  useEffect(() => {
    const timer = setTimeout(() => {
      const iframe = document.querySelector(CHAT_FRAME_SELECTOR);
      if (iframe && iframe.contentWindow && user) {
        iframe.contentWindow.postMessage(userContextMessage(contextRef.current), new URL(iframe.src).origin);
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [user, userGroups, isAdmin, callId]);

  useEffect(() => {
    const gql = clientRef.current;
    // The answers still streaming; closed when the host goes away.
    const subscriptions = new Set();

    const handleMessage = async (event) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data || {};
      const reply = (message) => event.source?.postMessage(message, event.origin);

      if (data.type === 'STRANDS_REQUEST_USER_CONTEXT') {
        reply(userContextMessage(contextRef.current));
      } else if (data.type === 'STRANDS_BUTTON_CONFIG_REQUEST' && contextRef.current.role === PARTICIPANT) {
        // The chat buttons are read by Admins and Users only; a Participant's
        // frame shows the Participant set, without a request the API would refuse.
        reply({
          type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
          requestId: data.requestId,
          success: true,
          config: { ...PARTICIPANT_CHAT_BUTTONS },
        });
      } else if (data.type === 'STRANDS_BUTTON_CONFIG_REQUEST') {
        try {
          const result = await gql.graphql({
            query: GET_CHAT_BUTTON_CONFIG,
            variables: { defaultId: 'DefaultChatButtonConfig', customId: 'CustomChatButtonConfig' },
          });
          reply({
            type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
            requestId: data.requestId,
            success: true,
            config: { ...extraButtonsRef.current, ...mergeChatButtons(result) },
          });
        } catch (error) {
          logger.error('getChatButtonConfig call failed', error);
          reply({
            type: 'STRANDS_BUTTON_CONFIG_RESPONSE',
            requestId: data.requestId,
            success: false,
            error: errorText(error, 'getChatButtonConfig call failed'),
          });
        }
      } else if (data.type === 'STRANDS_UPDATE_BUTTON_CONFIG_REQUEST') {
        // Nothing in LMA sends this message: strands-chat.html only reads its
        // buttons (STRANDS_BUTTON_CONFIG_REQUEST), and they are edited from the
        // Edit Chat Buttons dialog, which sends the version it loaded. This path
        // sends none, so its save is conditional on the record as the resolver
        // reads it at save time but not on what the sender was looking at; a
        // sender that wants that check has to supply the version itself.
        try {
          const result = await gql.graphql({
            query: UPDATE_CHAT_BUTTON_CONFIG,
            variables: { input: { ChatButtonConfigId: 'CustomChatButtonConfig', ButtonConfig: data.buttonConfig } },
          });
          reply({ type: 'STRANDS_UPDATE_BUTTON_CONFIG_RESPONSE', requestId: data.requestId, success: true, result });
        } catch (error) {
          logger.error('updateChatButtonConfig call failed', error);
          reply({
            type: 'STRANDS_UPDATE_BUTTON_CONFIG_RESPONSE',
            requestId: data.requestId,
            success: false,
            error: errorText(error, 'updateChatButtonConfig call failed'),
          });
        }
      } else if (data.type === 'STRANDS_CHAT_REQUEST') {
        try {
          const result = await gql.graphql({
            query: SEND_CHAT_MESSAGE,
            variables: {
              input: {
                CallId: data.callId,
                Message: data.message,
                ConversationHistory: data.conversationHistory || [],
              },
            },
          });
          reply({ type: 'STRANDS_CHAT_RESPONSE', messageId: data.messageId, success: true, result });
        } catch (error) {
          logger.error('sendChatMessage call failed', error);
          reply({
            type: 'STRANDS_CHAT_RESPONSE',
            messageId: data.messageId,
            success: false,
            error: errorText(error, 'sendChatMessage call failed'),
          });
        }
      } else if (data.type === 'STRANDS_SETUP_TOKEN_SUBSCRIPTION') {
        try {
          let subscription = null;
          let finished = false;
          const close = () => {
            finished = true;
            if (!subscription) return;
            subscription.unsubscribe?.();
            subscriptions.delete(subscription);
          };
          subscription = gql
            .graphql({ query: ON_ADD_CHAT_TOKEN, variables: { callId: data.callId, messageId: data.messageId } })
            .subscribe({
              next: (message) => {
                const token = message?.data?.onAddChatToken;
                if (!token) return;
                reply({ type: 'STRANDS_TOKEN_MESSAGE', token });
                // One subscription per answer: close it once the answer is complete.
                if (token.IsComplete) {
                  close();
                  onAnswerCompleteRef.current?.();
                }
              },
              error: (error) => logger.error('Token subscription error', error),
            });
          if (finished) {
            // The final token arrived before subscribe() returned.
            subscription.unsubscribe?.();
          } else {
            subscriptions.add(subscription);
          }
        } catch (error) {
          logger.error('Failed to set up token subscription', error);
        }
      }
    };

    window.addEventListener('message', handleMessage);
    return () => {
      window.removeEventListener('message', handleMessage);
      subscriptions.forEach((s) => s?.unsubscribe?.());
    };
  }, []);
};

export default useStrandsChatBridge;
