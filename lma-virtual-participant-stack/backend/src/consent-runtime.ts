/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting consent flow (meeting-consent.ts) wired to this Virtual
 * Participant: the `details.start` switch, the transcription service, the
 * screen recorder, the status line, the recordings bucket and the consent API.
 *
 * Kept apart from the flow itself so the flow's tests do not load the
 * transcription service and the recorders.
 */
import type { Page } from 'playwright-core';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { details, exitMessagesFor } from './details.js';
import { ConfigurationStore, declareSection } from './configuration-store.js';
import { createConsentApi, signedGraphqlSender } from './consent-api.js';
import { captureMeetingPanels, EvidencePanels } from './consent-evidence.js';
import { MeetingConsentFlow, VpConsentChoice } from './meeting-consent.js';
import { rosterLagSeconds, transcriptionService } from './scribe.js';
import { ConsentListener } from './consent-listener.js';
import { videoRecorder } from './video-recorder.js';
import { kinesisStreamManager } from './kinesis-stream.js';
import { createStatusManager } from './status-manager.js';
import { createLogger } from './logger.js';

const log = createLogger('meeting-consent');

const REGION = process.env.AWS_REGION || process.env.VP_AWS_REGION || 'us-east-1';

/**
 * The Meeting Consent section, declared on the immediate tier as the Python
 * half declares it (lma_config/sections.py): it decides whether a meeting may
 * be captured, so it is never served from a cache.
 */
export const MEETING_CONSENT = declareSection({
    name: 'meeting_consent',
    cacheTier: 'immediate',
    description:
        'Meeting consent: the default mode (off, notify or opt-in) and opt-in rule, the notice texts, the consent and ' +
        'objection phrases, whether a meeting may use another choice, and whether a Virtual Participant leaves after an objection.',
});

const graphqlEndpoint = (): string => process.env.GRAPHQL_ENDPOINT || '';

/** How long one read of this VP's consent choice may take before joining. */
const VP_CHOICE_READ_TIMEOUT_MS = 5_000;

const s3 = new S3Client({ region: REGION });

/**
 * Listens for spoken consent replies while the flow withholds capture, on the
 * meeting's transcription engine (consent-listener.ts). What it hears goes to
 * the flow only.
 */
const consentListener = new ConsentListener({
    engines: transcriptionService.consentListenerEngines(),
    ownIdentities: [details.lmaIdentity, details.scribeIdentity, details.scribeName],
    lagSeconds: () => rosterLagSeconds(),
    currentSpeaker: () => transcriptionService.participantSpeaker,
});

export const meetingConsent = new MeetingConsentFlow({
    capture: {
        isOn: () => details.start,
        hold: () => {
            details.start = false;
        },
        listen: (on) => {
            if (on) consentListener.start();
            else void consentListener.stop();
        },
        release: () => {
            details.start = true;
            // Resolves only when the meeting ends, as at every other call site.
            void transcriptionService.startTranscription();
        },
        // Keyed by VP_CALL_ID, as index.ts keys the recording it starts itself,
        // or else by the CallId the meeting's events carry.
        startVideo: () => videoRecorder.start(process.env.VP_CALL_ID || kinesisStreamManager.currentCallId),
        stopVideo: async () => {
            const url = await videoRecorder.stop();
            if (url) await kinesisStreamManager.sendCallVideoRecording(url);
        },
    },
    api: () => {
        const callId = kinesisStreamManager.currentCallId;
        if (!graphqlEndpoint() || !callId) return null;
        return createConsentApi(callId, signedGraphqlSender(graphqlEndpoint(), REGION));
    },
    setStatusDetail: async (detail) => {
        const vpId = details.invite.virtualParticipantId;
        if (!vpId || process.env.LOCAL_TEST === 'true') return;
        await createStatusManager(vpId).setStatusDetail(detail);
    },
    createMeeting: () => kinesisStreamManager.sendStartMeeting(),
    markJoined: async () => {
        const vpId = details.invite.virtualParticipantId;
        if (!vpId || process.env.LOCAL_TEST === 'true') return;
        await createStatusManager(vpId).setJoined();
    },
    upload: async (key, body) => {
        await s3.send(
            new PutObjectCommand({ Bucket: details.recordingsBucketName, Key: key, Body: body, ContentType: 'image/jpeg' }),
        );
    },
    callId: () => kinesisStreamManager.currentCallId,
    lmaUser: details.lmaUser,
    meetingName: details.invite.meetingName,
    ownIdentities: [details.lmaIdentity, details.scribeIdentity, details.scribeName],
    startMessages: () => details.startMessages,
    exitMessages: () => exitMessagesFor(null),
    every: (fn, ms) => {
        const handle = setInterval(fn, ms);
        return () => clearInterval(handle);
    },
});

/** The consent choice stored on this VP's row when it was created. */
async function readVpChoice(): Promise<VpConsentChoice | null> {
    const vpId = details.invite.virtualParticipantId;
    if (!vpId || !graphqlEndpoint()) return null;
    // Short: this read holds up the join, and one that fails is retried after
    // joining with capture withheld (meeting-consent.ts), so nothing is lost.
    const send = signedGraphqlSender(graphqlEndpoint(), REGION, VP_CHOICE_READ_TIMEOUT_MS);
    const data = await send(
        'query GetVirtualParticipant($id: ID!) { getVirtualParticipant(id: $id) { id consentMode consentRule } }',
        { id: vpId },
    );
    const row = (data.getVirtualParticipant || null) as { consentMode?: string | null; consentRule?: string | null } | null;
    return row ? { mode: row.consentMode ?? null, rule: row.consentRule ?? null } : null;
}

/** The deployment's Meeting Consent section, read with no cache. Throws when it cannot be read. */
async function readConsentSection(): Promise<Record<string, unknown>> {
    const tableName = process.env.CONFIGURATION_TABLE_NAME || '';
    // Not "nothing configured": a section that cannot be read must not read as Off.
    if (!tableName) throw new Error('CONFIGURATION_TABLE_NAME is not set');
    const store = new ConfigurationStore({ tableName, client: new DynamoDBClient({ region: REGION }) });
    const section = await store.getSection<Record<string, unknown>>(MEETING_CONSENT);
    return section.values;
}

/**
 * Before joining: works out the meeting's consent mode, and withholds capture
 * for an opt-in meeting. Answers whether the screen recording waits for
 * consent. A local test run has no API and runs no consent flow.
 */
export async function prepareMeetingConsent(): Promise<{ holdCapture: boolean; deferVideo: boolean }> {
    if (process.env.LOCAL_TEST === 'true' || !graphqlEndpoint()) {
        log.info('[consent] no AppSync endpoint (local test); the consent flow does not run');
        return { holdCapture: false, deferVideo: false };
    }
    return meetingConsent.prepare(readVpChoice, readConsentSection);
}

/**
 * Called by each platform handler once it is in the meeting and reading its
 * chat: posts the notice and runs the flow from there. A no-op when the
 * meeting runs no consent flow.
 */
export function attachMeetingConsent(
    page: Page,
    options: {
        /** Posts in the meeting chat; true only when every message was posted. Never throws. */
        send: (messages: string[]) => Promise<boolean>;
        leave: (objector: string) => void;
        panels?: EvidencePanels;
        readRoster?: () => Promise<string[] | null>;
        sendersHidden?: boolean;
    },
): void {
    meetingConsent.attach({
        sendersHidden: options.sendersHidden,
        sendMessages: options.send,
        screenshots: () => captureMeetingPanels(page, options.panels),
        readRoster: options.readRoster,
        leave: options.leave,
    });
}
