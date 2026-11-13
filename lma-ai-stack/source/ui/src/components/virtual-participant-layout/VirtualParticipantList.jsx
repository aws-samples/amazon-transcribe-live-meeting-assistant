/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import { fetchAuthSession, fetchUserAttributes } from 'aws-amplify/auth';
import React, { useState, useEffect, useCallback } from 'react';
import {
  Table,
  Box,
  SpaceBetween,
  Button,
  Modal,
  Form,
  FormField,
  Alert,
  Flashbar,
  Checkbox,
  Textarea,
  Pagination,
  TextFilter,
} from '@cloudscape-design/components';
import { SFNClient, StartSyncExecutionCommand } from '@aws-sdk/client-sfn';
import { useCollection } from '@cloudscape-design/collection-hooks';

import useAppContext from '../../contexts/app';
import useUserGroups from '../../hooks/use-user-groups';
import useMeetingConsentDefaults from '../../hooks/use-meeting-consent-defaults';
import awsExports from '../../aws-exports';
import useSettingsContext from '../../contexts/settings';
import useLocalStorage from '../common/local-storage';
import { paginationLabels } from '../common/labels';
import { getFilterCounterText, TableEmptyState, TableNoMatchState } from '../common/table';

import {
  VPCommonHeader,
  VPPreferences,
  COLUMN_DEFINITIONS_MAIN,
  KEY_COLUMN_ID,
  SELECTION_LABELS,
  DEFAULT_PREFERENCES,
  DEFAULT_SORT_COLUMN,
  TIME_FILTER_STORAGE_KEY,
  filterVPsByTime,
} from './vp-table-config';
import ZoomCredentialsManager from './ZoomCredentialsManager';
import ChromeProfileManager from './ChromeProfileManager';
import { meetingConsentChoiceAvailable, meetingConsentInput } from './meeting-consent-options';
import VirtualParticipantFormFields from './VirtualParticipantFormFields';
import {
  CREATE_MIN_LEAD_MINUTES,
  EMPTY_VP_FORM,
  browserTimeZone,
  formFieldsAreValid,
  formMeetingTimestamp,
  meetingTimeError,
  submittedMeetingFields,
} from './vp-form';

import '@cloudscape-design/global-styles/index.css';

const client = generateClient();
const logger = new ConsoleLogger('VirtualParticipantList');

const listVirtualParticipants = `
  query ListVirtualParticipants {
    listVirtualParticipants {
      id
      meetingName
      meetingPlatform
      meetingId
      meetingTime
      scheduledFor
      isScheduled
      status
      owner
      Owner
      SharedWith
      createdAt
      updatedAt
    }
  }
`;

const createVirtualParticipant = /* GraphQL */ `
  mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
    createVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      status
      Owner
      SharedWith
      createdAt
    }
  }
`;

const parseMeetingInvitation = /* GraphQL */ `
  query ParseMeetingInvitation($invitationText: String!) {
    parseMeetingInvitation(invitationText: $invitationText)
  }
`;

const VirtualParticipantList = () => {
  const { user, currentCredentials } = useAppContext();
  const { isAdmin } = useUserGroups();
  const { settings } = useSettingsContext();
  const zoomSdkEnabled = String(settings?.ZoomSdkEnabled ?? '').toLowerCase() === 'true';

  const [participants, setParticipants] = useState([]);
  const [filteredParticipants, setFilteredParticipants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  // Why a parsed invitation left the meeting ID empty, shown on the field itself.
  const [meetingIdNote, setMeetingIdNote] = useState('');
  // The note belongs to the invitation just parsed: it goes when the form
  // closes (cancel, dismiss or create), and when another platform is chosen.
  useEffect(() => {
    if (!showCreateModal) setMeetingIdNote('');
  }, [showCreateModal]);
  const [createForm, setCreateForm] = useState(EMPTY_VP_FORM);
  const [consentChecked, setConsentChecked] = useState(false);
  // Per-VP screen-recording toggle. Default ON (matches the ENABLE_VIDEO_RECORDING
  // task-definition default); users untick to launch a VP that records audio +
  // transcript only.
  const [enableVideoRecording, setEnableVideoRecording] = useState(true);
  const [meetingConsentChoice, setMeetingConsentChoice] = useState('default');
  // Read each time the form opens. Null until read, or when it cannot be:
  // every choice is then offered, and the VP still applies the deployment's rule.
  const consentDefaults = useMeetingConsentDefaults(showCreateModal);
  // A choice the selected platform cannot support, or the deployment would not
  // use, goes back to the default.
  useEffect(() => {
    if (!meetingConsentChoiceAvailable(meetingConsentChoice, createForm.meetingPlatform, consentDefaults)) {
      setMeetingConsentChoice('default');
    }
  }, [createForm.meetingPlatform, meetingConsentChoice, consentDefaults]);
  const [notification, setNotification] = useState(null);
  const [isCreating, setIsCreating] = useState(false);
  const [creatingType, setCreatingType] = useState(null);
  const [popupNotifications, setPopupNotifications] = useState([]);
  const [zoomCredsStatus, setZoomCredsStatus] = useState({ present: false, username: null, lastUpdatedAt: null });
  // Default ON when credentials are stored — flipped automatically by the
  // ZoomCredentialsManager onChange callback. Users can untick per meeting.
  const [useZoomLogin, setUseZoomLogin] = useState(false);

  // Stable identity so ZoomCredentialsManager's refresh() useEffect doesn't
  // re-run on every parent render and cause flicker.
  const handleZoomCredsChange = useCallback((status) => {
    setZoomCredsStatus(status);
    if (status?.present) setUseZoomLogin(true);
  }, []);

  // Meeting invitation parser state
  const [showPasteInviteModal, setShowPasteInviteModal] = useState(false);
  const [invitationText, setInvitationText] = useState('');
  const [isParsing, setIsParsing] = useState(false);
  const [parseError, setParseError] = useState('');

  // Advanced table features
  const [preferences, setPreferences] = useLocalStorage('vp-list-preferences', DEFAULT_PREFERENCES);
  const [timeFilter, setTimeFilter] = useLocalStorage(TIME_FILTER_STORAGE_KEY, 24); // Default to 1 day

  // Collection hooks for advanced table functionality
  const { items, actions, filteredItemsCount, collectionProps, filterProps, paginationProps } = useCollection(
    filteredParticipants,
    {
      filtering: {
        empty: <TableEmptyState resourceName="Virtual Participant" />,
        noMatch: <TableNoMatchState onClearFilter={() => actions.setFiltering('')} />,
      },
      pagination: { pageSize: preferences.pageSize },
      sorting: { defaultState: { sortingColumn: DEFAULT_SORT_COLUMN, isDescending: true } },
      selection: {
        keepSelection: false,
        trackBy: KEY_COLUMN_ID,
      },
    },
  );

  const loadParticipants = async () => {
    try {
      setLoading(true);
      logger.debug('Loading virtual participants');
      const result = await client.graphql({ query: listVirtualParticipants });
      const vpList = result.data.listVirtualParticipants || [];
      logger.debug('Loaded virtual participants:', vpList);
      setParticipants(vpList);
    } catch (error) {
      logger.error('Failed to load virtual participants:', error);
      setNotification({
        type: 'error',
        content: 'Failed to load virtual participants',
      });
    } finally {
      setLoading(false);
    }
  };

  // Apply time filtering whenever participants or timeFilter changes
  useEffect(() => {
    logger.debug('Applying time filter:', timeFilter);
    const filtered = filterVPsByTime(participants, timeFilter);
    logger.debug('Filtered participants:', filtered.length, 'of', participants.length);
    setFilteredParticipants(filtered);
  }, [participants, timeFilter]);

  // Load participants on component mount
  useEffect(() => {
    loadParticipants();
  }, []);

  useEffect(() => {
    const onCreateVirtualParticipant = /* GraphQL */ `
      subscription OnCreateVirtualParticipant {
        onCreateVirtualParticipant {
          id
          meetingName
          meetingPlatform
          meetingId
          meetingTime
          scheduledFor
          isScheduled
          status
          owner
          Owner
          SharedWith
          createdAt
          updatedAt
        }
      }
    `;
    const onUpdateVirtualParticipant = /* GraphQL */ `
      subscription OnUpdateVirtualParticipant {
        onUpdateVirtualParticipant {
          id
          meetingName
          meetingPlatform
          meetingId
          meetingTime
          scheduledFor
          isScheduled
          status
          owner
          Owner
          SharedWith
          createdAt
          updatedAt
        }
      }
    `;

    const createSubscription = client.graphql({ query: onCreateVirtualParticipant }).subscribe({
      next: (subscriptionMessage) => {
        const created = subscriptionMessage?.data?.onCreateVirtualParticipant;
        if (!created || !created.id) return;
        setParticipants((prev) => {
          // Idempotent: don't double-add if already present (covers the
          // case where loadParticipants raced with the subscription).
          if (prev.some((p) => p.id === created.id)) return prev;
          return [created, ...prev];
        });
      },
      error: (error) => {
        logger.error('Create subscription error:', error);
      },
    });

    const subscription = client.graphql({ query: onUpdateVirtualParticipant }).subscribe({
      next: (subscriptionMessage) => {
        const updatedParticipant = subscriptionMessage?.data?.onUpdateVirtualParticipant;

        if (!updatedParticipant || !updatedParticipant.id) {
          return;
        }

        setParticipants((prev) => {
          const existingParticipant = prev.find((p) => p.id === updatedParticipant.id);
          const meetingName = updatedParticipant.meetingName || existingParticipant?.meetingName || 'Unknown';
          // Only show notification if status actually changed
          if (
            existingParticipant &&
            existingParticipant.status !== updatedParticipant.status &&
            updatedParticipant.status
          ) {
            let notificationType = 'info';
            let message = '';

            switch (updatedParticipant.status) {
              case 'INITIALIZING':
                notificationType = 'info';
                message = `Virtual Participant initializing for "${meetingName}"`;
                break;
              case 'CONNECTING':
                notificationType = 'info';
                message = `Virtual Participant connecting to "${meetingName}"`;
                break;
              case 'JOINING':
                notificationType = 'info';
                message = `Virtual Participant joining "${meetingName}"`;
                break;
              case 'JOINED':
                notificationType = 'success';
                message = `Virtual Participant joined "${meetingName}"`;
                break;
              case 'ACTIVE':
                notificationType = 'success';
                message = `Virtual Participant is active in "${meetingName}"`;
                break;
              case 'COMPLETED':
                notificationType = 'success';
                message = `Virtual Participant completed "${meetingName}"`;
                break;
              case 'FAILED':
                notificationType = 'error';
                message = `Virtual Participant failed to join "${meetingName}"`;
                break;
              default:
                message = `Virtual Participant status updated to ${updatedParticipant.status} for "${meetingName}"`;
            }

            // Add popup notification
            const notificationId = `vp-${updatedParticipant.id}-${updatedParticipant.status}-${Date.now()}`;
            const popupNotification = {
              type: notificationType,
              content: message,
              dismissible: true,
              dismissLabel: 'Dismiss',
              id: notificationId,
              onDismiss: () => {
                setPopupNotifications((notifications) => notifications.filter((n) => n.id !== notificationId));
              },
            };

            setPopupNotifications((current) => [...current, popupNotification]);

            // Auto-dismiss after 8 seconds
            setTimeout(() => {
              setPopupNotifications((notifications) => notifications.filter((n) => n.id !== notificationId));
            }, 8000);
          }

          // If we don't already have this VP in the list, insert it
          // (covers the case where the create-subscription event was
          // dropped due to a reconnect / late mount). Better to show a
          // partial row than silently leave it off the list.
          if (!existingParticipant) {
            return [
              {
                id: updatedParticipant.id,
                status: updatedParticipant.status,
                updatedAt: updatedParticipant.updatedAt,
                meetingName: updatedParticipant.meetingName,
                Owner: updatedParticipant.Owner,
                owner: updatedParticipant.owner,
                SharedWith: updatedParticipant.SharedWith,
                meetingPlatform: updatedParticipant.meetingPlatform,
                meetingId: updatedParticipant.meetingId,
                meetingTime: updatedParticipant.meetingTime,
                scheduledFor: updatedParticipant.scheduledFor,
                isScheduled: updatedParticipant.isScheduled,
                createdAt: updatedParticipant.createdAt,
              },
              ...prev,
            ];
          }
          return prev.map((p) => {
            if (p.id === updatedParticipant.id) {
              return {
                ...p,
                status: updatedParticipant.status,
                updatedAt: updatedParticipant.updatedAt,
              };
            }
            return p;
          });
        });
      },
      error: (error) => {
        logger.error('Subscription error:', error);
        // Fallback to polling
        const pollInterval = setInterval(() => {
          loadParticipants();
        }, 5000);

        return () => clearInterval(pollInterval);
      },
    });

    return () => {
      try {
        createSubscription.unsubscribe();
      } catch (_) {
        /* ignore */
      }
      try {
        subscription.unsubscribe();
      } catch (_) {
        /* ignore */
      }
    };
  }, []);

  // Handle parsing meeting invitation
  const handleParseMeetingInvitation = async () => {
    if (!invitationText.trim()) {
      setParseError('Please paste a meeting invitation');
      return;
    }

    setIsParsing(true);
    setParseError('');

    try {
      const result = await client.graphql({
        query: parseMeetingInvitation,
        variables: {
          invitationText: invitationText.trim(),
        },
      });

      const parsedResponse = JSON.parse(result.data.parseMeetingInvitation);

      if (parsedResponse.success && parsedResponse.data) {
        const { data } = parsedResponse;

        // Auto-fill the form with parsed data
        setCreateForm((prev) => ({
          ...prev,
          meetingName: data.meetingName || prev.meetingName,
          meetingPlatform: data.meetingPlatform || prev.meetingPlatform,
          meetingId: data.meetingId || prev.meetingId,
          meetingPassword: data.meetingPassword || prev.meetingPassword,
          meetingDate: data.meetingDate || prev.meetingDate,
          meetingTime: data.meetingTime || prev.meetingTime,
          // Only the agenda the invitation lists; the parser is told not to
          // invent one, and leaves it null when there is none.
          agenda: data.agenda || prev.agenda,
        }));

        // Close paste invite modal and open create modal
        setShowPasteInviteModal(false);
        setInvitationText('');
        setShowCreateModal(true);

        // The parser leaves out a meeting ID the form cannot use, and says why;
        // the reason is shown on the Meeting ID field, inside the form.
        setMeetingIdNote(data.meetingIdNote || '');
        setNotification(
          data.meetingIdNote
            ? null
            : {
                type: 'success',
                content: 'Meeting invitation parsed successfully! Please review and verify the details.',
              },
        );
      } else {
        setParseError(parsedResponse.error || 'Failed to parse meeting invitation');
      }
    } catch (error) {
      console.error('Error parsing meeting invitation:', error);
      setParseError('Failed to parse meeting invitation. Please try again or enter details manually.');
    } finally {
      setIsParsing(false);
    }
  };

  const parseStepFunctionError = (executionResult) => {
    const { output } = executionResult;
    const parsedOutput = output ? JSON.parse(output) : {};
    const errorMessage = parsedOutput.errorMessage || parsedOutput.error || '';

    // Check for specific error patterns
    if (
      errorMessage.toLowerCase().includes('meeting not found') ||
      errorMessage.toLowerCase().includes('invalid meeting id')
    ) {
      return 'Meeting ID not found. Please check the meeting ID and try again.';
    }

    if (
      errorMessage.toLowerCase().includes('incorrect password') ||
      errorMessage.toLowerCase().includes('authentication failed')
    ) {
      return 'Incorrect meeting password. Please check the password and try again.';
    }

    if (
      errorMessage.toLowerCase().includes('meeting not started') ||
      errorMessage.toLowerCase().includes('meeting has not begun')
    ) {
      return 'Meeting has not started yet. Please wait for the host to start the meeting.';
    }

    if (
      errorMessage.toLowerCase().includes('meeting ended') ||
      errorMessage.toLowerCase().includes('meeting has ended')
    ) {
      return 'This meeting has already ended. Please check the meeting details.';
    }

    if (
      errorMessage.toLowerCase().includes('permission denied') ||
      errorMessage.toLowerCase().includes('not authorized')
    ) {
      return 'Permission denied. You may not have access to join this meeting.';
    }

    // Generic error
    return errorMessage || 'Failed to join meeting. Please check your meeting details and try again.';
  };

  const handleCreateParticipant = async (isScheduled = false) => {
    setIsCreating(true);
    setCreatingType(isScheduled ? 'scheduled' : 'immediate');

    try {
      let email;
      let cognitoSub;
      try {
        const attrs = await fetchUserAttributes();
        email = attrs?.email;
        cognitoSub = attrs?.sub;
      } catch (attrErr) {
        logger.debug('fetchUserAttributes failed, falling back to user identifier', attrErr);
      }
      const userName =
        email || user?.attributes?.email || user?.signInDetails?.loginId || user?.username || 'test-user@example.com';
      // userSub is the Cognito sub of the launching user. The VP backend
      // uses it for two things:
      //   1. Persistent Chromium profile path (profiles/{sub}/{platform}/)
      //      — always passed when present so cookies/trusted-device markers
      //      survive across meetings on any platform.
      //   2. Resolving ZOOM_CREDENTIALS_SECRET_NAME (the state machine
      //      prepends `${LMAStackName}/zoom-credentials/`) — only when the
      //      user explicitly opted into Zoom login for this meeting.
      const userSub = cognitoSub || '';
      const userZoomSub =
        createForm.meetingPlatform === 'ZOOM' && useZoomLogin && zoomCredsStatus.present && cognitoSub
          ? cognitoSub
          : '';

      // Calculate meeting time for scheduling
      let meetingTimestamp = null;

      if (isScheduled) {
        meetingTimestamp = formMeetingTimestamp(createForm.meetingDate, createForm.meetingTime);
      }

      // A join URL carries its password server-side and the password field is
      // hidden, so never submit a (possibly stale) separate password with one.
      // Google Meet has no meeting password, and is stored as its meeting code.
      const { meetingId: effectiveMeetingId, meetingPassword: effectivePassword } = submittedMeetingFields(createForm);

      // Create VP record with scheduling information
      const vpInput = {
        meetingName: createForm.meetingName,
        meetingPlatform: createForm.meetingPlatform,
        meetingId: effectiveMeetingId,
        meetingPassword: effectivePassword,
        status: isScheduled ? 'SCHEDULED' : 'INITIALIZING',
        // Persisted on the row so the VPScheduler Lambda can plumb
        // ENABLE_VIDEO_RECORDING to scheduled launches. Immediate launches
        // carry it via the Step Functions input below.
        enableVideoRecording,
      };

      // Stored on the VP row for immediate and scheduled meetings alike; the VP
      // reads it from there when it starts and puts it on the meeting record.
      const agendaText = (createForm.agenda || '').trim();
      if (agendaText) vpInput.agenda = agendaText;

      // Stored on the VP row too, and read by the VP when it starts.
      Object.assign(vpInput, meetingConsentInput(meetingConsentChoice));

      // Recorded so the Virtual Participant page can show the Zoom account
      // choice; only where the form offers it.
      if (createForm.meetingPlatform === 'ZOOM' && !zoomSdkEnabled) {
        vpInput.useStoredZoomAccount = !!userZoomSub;
      }

      // Add scheduling fields if this is a scheduled VP
      if (isScheduled && meetingTimestamp) {
        vpInput.meetingTime = meetingTimestamp;
        vpInput.isScheduled = true;
        // The date and time fields are read in this browser's time zone; it is
        // recorded so the Virtual Participant page can show the time as entered.
        const timeZone = browserTimeZone();
        if (timeZone) vpInput.scheduledTimeZone = timeZone;
        // Persist userSub / userZoomSub on the row so the VPScheduler
        // Lambda can plumb LMA_USER_SUB and ZOOM_CREDENTIALS_SECRET_NAME
        // to the launched ECS task at meeting time. Immediate (non-scheduled)
        // meetings carry these via the Step Functions input below.
        if (userSub) vpInput.userSub = userSub;
        if (userZoomSub) vpInput.userZoomSub = userZoomSub;
      }

      const vpResult = await client.graphql({
        query: createVirtualParticipant,
        variables: {
          input: vpInput,
        },
      });

      const virtualParticipantId = vpResult.data.createVirtualParticipant.id;

      // For immediate execution, still use Step Functions (backward compatibility)
      if (!isScheduled) {
        const sfnClient = new SFNClient({
          region: awsExports.aws_project_region,
          credentials: currentCredentials,
        });

        const sfnParams = {
          stateMachineArn: settings.LMAVirtualParticipantSchedulerStateMachine,
          input: JSON.stringify({
            apiInfo: { httpMethod: 'POST' },
            data: {
              meetingPlatform: createForm.meetingPlatform,
              meetingID: effectiveMeetingId,
              meetingPassword: effectivePassword,
              meetingName: createForm.meetingName,
              meetingTime: '',
              userName,
              virtualParticipantId,
              userSub,
              userZoomSub,
              // ECS container Environment values must be strings, not booleans.
              enableVideoRecording: enableVideoRecording ? 'true' : 'false',
              accessToken: (await fetchAuthSession()).tokens?.accessToken?.toString() || '',
              idToken: (await fetchAuthSession()).tokens?.idToken?.toString() || '',
              rereshToken: '', // Amplify v6 does not expose refresh tokens directly
            },
          }),
        };

        const data = await sfnClient.send(new StartSyncExecutionCommand(sfnParams));

        if (data.status === 'FAILED') {
          const errorMessage = parseStepFunctionError(data);
          setNotification({
            type: 'error',
            content: errorMessage,
          });
          return;
        }

        if (data.status === 'SUCCEEDED') {
          const { output } = data;
          const parsedOutput = output ? JSON.parse(output) : {};

          if (parsedOutput.success === false || parsedOutput.error) {
            const errorMessage = parseStepFunctionError(data);
            setNotification({
              type: 'error',
              content: errorMessage,
            });
            return;
          }
        }
      }

      // Reset form and close modal
      setShowCreateModal(false);
      setCreateForm(EMPTY_VP_FORM);
      setConsentChecked(false);
      setEnableVideoRecording(true);
      setMeetingConsentChoice('default');

      loadParticipants();

      // Show appropriate success message
      const scheduledDateTime = new Date(`${createForm.meetingDate}T${createForm.meetingTime}`);
      const successMessage = isScheduled
        ? `Virtual participant "${
            createForm.meetingName
          }" scheduled successfully for ${scheduledDateTime.toLocaleString()}.`
        : `Virtual participant "${createForm.meetingName}" started successfully and is joining the meeting.`;

      setNotification({
        type: 'success',
        content: successMessage,
      });
    } catch (err) {
      const { message: errorMessage } = err;
      if (errorMessage.includes('StateMachineDoesNotExist')) {
        setNotification({
          type: 'error',
          content: 'Virtual Participant service is not configured. Please contact your administrator.',
        });
      } else if (errorMessage.includes('AccessDenied')) {
        setNotification({
          type: 'error',
          content: 'You do not have permission to start a virtual participant. Please contact your administrator.',
        });
      } else if (errorMessage.includes('InvalidParameterValue')) {
        setNotification({
          type: 'error',
          content: 'Invalid meeting information provided. Please check your inputs and try again.',
        });
      } else if (errorMessage.includes('GraphQL')) {
        setNotification({
          type: 'error',
          content: 'Failed to create virtual participant record. Please try again.',
        });
      } else {
        setNotification({
          type: 'error',
          content: 'Failed to start virtual participant. Please check your meeting details and try again.',
        });
      }
    } finally {
      setIsCreating(false);
      setCreatingType(null);
    }
  };

  // The date and time are read in this browser's time zone.
  const meetingTimeErrorText = meetingTimeError(
    createForm.meetingDate,
    createForm.meetingTime,
    CREATE_MIN_LEAD_MINUTES,
  );

  // Check if form is ready for immediate execution
  const isFormValidForImmediate = formFieldsAreValid(createForm) && consentChecked;

  // Check if form is ready for scheduling
  const isFormValidForScheduling =
    isFormValidForImmediate && createForm.meetingTime && createForm.meetingDate && !meetingTimeErrorText;

  /* eslint-disable react/jsx-props-no-spreading */
  return (
    <SpaceBetween direction="vertical" size="l">
      {/* Popup notifications using Flashbar */}
      {popupNotifications.length > 0 && <Flashbar items={popupNotifications} />}

      {notification && (
        <Alert type={notification.type} dismissible onDismiss={() => setNotification(null)}>
          {notification.content}
        </Alert>
      )}

      <Table
        {...collectionProps}
        header={
          <VPCommonHeader
            resourceName="Virtual Participants"
            selectedItems={collectionProps.selectedItems}
            totalItems={filteredParticipants}
            loading={loading}
            timeFilter={timeFilter}
            setTimeFilter={setTimeFilter}
            onRefresh={loadParticipants}
            onPasteInvite={() => setShowPasteInviteModal(true)}
            onCreateVP={() => setShowCreateModal(true)}
            onNotify={setNotification}
            viewer={{ username: user?.username, isAdmin }}
            items={filteredParticipants}
          />
        }
        columnDefinitions={COLUMN_DEFINITIONS_MAIN}
        items={items}
        loading={loading}
        loadingText="Loading virtual participants"
        selectionType="multi"
        ariaLabels={SELECTION_LABELS}
        filter={
          <TextFilter
            {...filterProps}
            filteringAriaLabel="Filter virtual participants"
            filteringPlaceholder="Find virtual participants"
            countText={getFilterCounterText(filteredItemsCount)}
          />
        }
        wrapLines={preferences.wrapLines}
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={<VPPreferences preferences={preferences} setPreferences={setPreferences} />}
        trackBy={KEY_COLUMN_ID}
        visibleColumns={[KEY_COLUMN_ID, ...preferences.visibleContent]}
        resizableColumns
      />

      <Modal
        visible={showCreateModal}
        onDismiss={() => setShowCreateModal(false)}
        header="Create Virtual Participant"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowCreateModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => handleCreateParticipant(false)}
                disabled={!isFormValidForImmediate || isCreating}
                loading={isCreating && creatingType === 'immediate'}
                loadingText="Starting Virtual Participant..."
              >
                {isCreating && creatingType === 'immediate' ? 'Starting...' : 'Join Now'}
              </Button>
              <Button
                variant="primary"
                onClick={() => handleCreateParticipant(true)}
                disabled={!isFormValidForScheduling || isCreating}
                loading={isCreating && creatingType === 'scheduled'}
                loadingText="Scheduling Virtual Participant..."
              >
                {isCreating && creatingType === 'scheduled' ? 'Scheduling...' : 'Schedule for Later'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween direction="vertical" size="l">
            <VirtualParticipantFormFields
              form={createForm}
              onChange={(patch) => setCreateForm((prev) => ({ ...prev, ...patch }))}
              zoomSdkEnabled={zoomSdkEnabled}
              zoomAccountField={
                <FormField
                  label="Zoom account (Optional)"
                  description={
                    'Stored Zoom credentials let LMA join meetings as authenticated user. Two-factor and ' +
                    'CAPTCHA challenges still require manual action via the LMA viewer.'
                  }
                  stretch
                >
                  <SpaceBetween direction="vertical" size="s">
                    <ZoomCredentialsManager onChange={handleZoomCredsChange} />
                    {zoomCredsStatus.present && (
                      <Checkbox checked={useZoomLogin} onChange={({ detail }) => setUseZoomLogin(detail.checked)}>
                        Use my stored Zoom account when joining this meeting (recommended)
                      </Checkbox>
                    )}
                  </SpaceBetween>
                </FormField>
              }
              browserProfileField={
                <FormField
                  label="Stored browser profile (Optional)"
                  description={
                    'LMA reuses a saved browser profile for this meeting platform so cookies and trusted-device ' +
                    'markers persist. For Teams, this lets a CAPTCHA you solve once carry over to later joins. ' +
                    'Remove it to start fresh.'
                  }
                  stretch
                >
                  <ChromeProfileManager platform={createForm.meetingPlatform} />
                </FormField>
              }
              meetingIdNote={meetingIdNote}
              onMeetingIdNoteClear={() => setMeetingIdNote('')}
              meetingTimeError={meetingTimeErrorText}
              consentChoice={meetingConsentChoice}
              onConsentChoiceChange={setMeetingConsentChoice}
              consentDefaults={consentDefaults}
              enableVideoRecording={enableVideoRecording}
              onEnableVideoRecordingChange={setEnableVideoRecording}
            />

            <Checkbox onChange={({ detail }) => setConsentChecked(detail.checked)} checked={consentChecked}>
              I will not violate legal, corporate, or ethical restrictions that apply to meeting transcription and
              recording.
            </Checkbox>
          </SpaceBetween>
        </Form>
      </Modal>

      {/* Paste Meeting Invite Modal */}
      <Modal
        visible={showPasteInviteModal}
        onDismiss={() => {
          setShowPasteInviteModal(false);
          setInvitationText('');
          setParseError('');
          // Reset the form completely when modal is closed
          setCreateForm(EMPTY_VP_FORM);
          setConsentChecked(false);
        }}
        header="Paste Meeting Invitation"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowPasteInviteModal(false);
                  setInvitationText('');
                  setParseError('');
                  // Reset the form completely when modal is cancelled
                  setCreateForm(EMPTY_VP_FORM);
                  setConsentChecked(false);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleParseMeetingInvitation}
                disabled={!invitationText.trim() || isParsing}
                loading={isParsing}
                loadingText="Parsing invitation..."
              >
                {isParsing ? 'Parsing...' : 'Parse Invitation'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween direction="vertical" size="l">
            <FormField
              label="Meeting Invitation"
              description="Paste your meeting invitation text here. The AI will extract meeting details."
              stretch
            >
              <Textarea
                value={invitationText}
                onChange={({ detail }) => {
                  setInvitationText(detail.value);
                  setParseError('');
                }}
                placeholder="Paste your meeting invitation here...

Example:
Join Zoom Meeting
https://zoom.us/j/1234567890?pwd=abcdef

Meeting ID: 123 456 7890
Passcode: 123456

Topic: Weekly Team Standup
Time: Dec 15, 2024 02:00 PM Eastern Time (US and Canada)"
                rows={12}
              />
            </FormField>

            {parseError && <Alert type="error">{parseError}</Alert>}
          </SpaceBetween>
        </Form>
      </Modal>
    </SpaceBetween>
  );
};

export default VirtualParticipantList;
