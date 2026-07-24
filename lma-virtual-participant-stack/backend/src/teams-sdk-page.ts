/* eslint-disable @typescript-eslint/no-explicit-any */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { AzureCommunicationTokenCredential } from '@azure/communication-common';
import { LocalVideoStream } from '@azure/communication-calling';
import {
    CallWithChatComposite,
    createAzureCommunicationCallWithChatAdapter,
    toFlatCommunicationIdentifier,
} from '@azure/communication-react';

const w = window as any;

function log(m: string): void {
    try {
        console.log(`[LMA-TeamsSDK] ${m}`);
    } catch {
        /* noop */
    }
}

function htmlToText(html: string): string {
    try {
        const div = document.createElement('div');
        div.innerHTML = html;
        return (div.textContent || '').trim();
    } catch {
        return html;
    }
}

w.__lmaSdkReady = false;
w.__lmaJoinError = null;
w.__lmaCallEndReason = null;
w.__lmaActiveSpeaker = '';
w.__lmaChatQueue = [];
w.__lmaChatReady = false;

let adapter: any = null;
let call: any = null;
let rawCameraStarted = false;
const pendingOutgoingChat: string[] = [];

function getState(): any {
    try {
        return adapter ? adapter.getState() : null;
    } catch {
        return null;
    }
}

w.__lmaCallState = () => {
    const state = getState();
    if (!state) return { page: 'none', callState: 'None' };
    return { page: state.page || 'none', callState: (state.call && state.call.state) || 'None' };
};

w.__lmaParticipantCount = () => {
    const state = getState();
    if (!state || !state.call || !state.call.remoteParticipants) return -1;
    return Object.keys(state.call.remoteParticipants).length + 1;
};

w.__lmaSendChat = (message: string) => {
    if (!adapter || !w.__lmaChatReady) {
        pendingOutgoingChat.push(message);
        return;
    }
    adapter.sendMessage(message).catch((e: unknown) => log(`sendMessage error: ${e}`));
};

function flushOutgoingChat(): void {
    while (pendingOutgoingChat.length) {
        const m = pendingOutgoingChat.shift();
        if (m) adapter.sendMessage(m).catch((e: unknown) => log(`sendMessage error: ${e}`));
    }
}

w.__lmaSetMuted = (muted: boolean) => {
    if (!adapter) return;
    const p = muted ? adapter.mute() : adapter.unmute();
    p.catch((e: unknown) => log(`${muted ? 'mute' : 'unmute'} error: ${e}`));
};

w.__lmaCameraOn = () => {
    if (rawCameraStarted) return true;
    const state = getState();
    return !!(state && state.call && state.call.localVideoStreams && state.call.localVideoStreams.length > 0);
};

w.__lmaStartCamera = async () => {
    try {
        await adapter.startCamera();
        if (w.__lmaCameraOn()) {
            log('camera started via adapter');
            return true;
        }
    } catch (e) {
        log(`adapter.startCamera failed: ${e}`);
    }
    // No real camera device exists in the container; the Simli getUserMedia
    // override answers this call with the avatar stream, which we publish
    // through the raw media API on the underlying call.
    try {
        if (!call) throw new Error('no call handle');
        const stream = await navigator.mediaDevices.getUserMedia({ video: true });
        await call.startVideo(new LocalVideoStream(stream));
        rawCameraStarted = true;
        log('camera started via raw media stream');
        return true;
    } catch (e) {
        log(`raw media camera failed: ${e}`);
        return false;
    }
};

w.__lmaLeave = () => {
    try {
        if (adapter) adapter.leaveCall().catch(() => {});
    } catch (e) {
        log(`leave error: ${e}`);
    }
};

function participantName(identifier: any): string {
    const state = getState();
    if (!state || !state.call || !state.call.remoteParticipants) return '';
    try {
        const key = toFlatCommunicationIdentifier(identifier);
        const p = state.call.remoteParticipants[key];
        if (p && p.displayName) return p.displayName;
    } catch {
        /* fall through */
    }
    for (const p of Object.values(state.call.remoteParticipants) as any[]) {
        if (p && p.isSpeaking && p.displayName) return p.displayName;
    }
    return '';
}

function setupListeners(): void {
    adapter.on('callEnded', (e: any) => {
        w.__lmaCallEndReason = { code: e && e.code, subCode: e && e.subCode };
        let detail = '';
        try {
            detail = JSON.stringify(e);
        } catch {
            detail = String(e);
        }
        log(`callEnded code=${e && e.code} subCode=${e && e.subCode} detail=${detail.slice(0, 400)}`);
        try {
            const state = getState();
            const endReason = state && state.call && (state.call.callEndReason || state.endedCall?.callEndReason);
            if (endReason) log(`callEndReason from state: ${JSON.stringify(endReason).slice(0, 400)}`);
        } catch {
            /* noop */
        }
    });
    adapter.on('callError', (e: any) => {
        let detail = '';
        try {
            detail = JSON.stringify(e, Object.getOwnPropertyNames(e || {}));
        } catch {
            detail = String(e);
        }
        log(`callError: ${detail.slice(0, 500)}`);
    });
    adapter.on('diagnosticChanged', (e: any) => {
        try {
            log(`diagnostic: ${JSON.stringify(e).slice(0, 300)}`);
        } catch {
            /* noop */
        }
    });
    adapter.on('isSpeakingChanged', (e: any) => {
        if (!e || !e.isSpeaking) return;
        const name = participantName(e.identifier);
        if (name) w.__lmaActiveSpeaker = name;
    });
    adapter.on('chatInitialized', () => {
        w.__lmaChatReady = true;
        log('chat initialized');
        flushOutgoingChat();
    });
    adapter.on('messageReceived', (e: any) => {
        try {
            const msg = (e && e.message) || e;
            if (!msg) return;
            const sender = msg.sender || {};
            const selfId = (w.__lmaTeamsConfig || {}).acsUserId;
            if (selfId && sender.communicationUserId && sender.communicationUserId === selfId) return;
            const senderName = msg.senderDisplayName || '';
            const rawContent =
                (msg.content && (msg.content.message || msg.content)) || msg.message || '';
            const text =
                msg.type === 'html' || /<[a-z][\s\S]*>/i.test(String(rawContent))
                    ? htmlToText(String(rawContent))
                    : String(rawContent).trim();
            if (!text) return;
            w.__lmaChatQueue.push({ senderName, text });
        } catch (err) {
            log(`chat parse error: ${err}`);
        }
    });
}

w.__lmaTeamsJoin = () => {
    const cfg = w.__lmaTeamsConfig || {};
    try {
        call = adapter.joinCall({
            microphoneOn: !!cfg.wantMic,
            cameraOn: false,
        });
        log('joinCall invoked');
    } catch (e: any) {
        w.__lmaJoinError = { reason: (e && e.message) || 'joinCall failed' };
        log(`joinCall error: ${e}`);
    }
};

async function init(): Promise<void> {
    const cfg = w.__lmaTeamsConfig;
    if (!cfg || !cfg.token || !cfg.endpoint || !cfg.acsUserId) {
        w.__lmaJoinError = { reason: '__lmaTeamsConfig missing or incomplete' };
        log('config missing');
        return;
    }
    try {
        const credential = new AzureCommunicationTokenCredential(cfg.token);
        const locator = cfg.meetingLink
            ? { meetingLink: cfg.meetingLink }
            : { meetingId: cfg.meetingId, passcode: cfg.passcode || undefined };
        adapter = await createAzureCommunicationCallWithChatAdapter({
            endpoint: cfg.endpoint,
            userId: { communicationUserId: cfg.acsUserId },
            displayName: cfg.displayName || 'LMA',
            credential,
            locator: locator as any,
        });
        setupListeners();

        const container = document.getElementById('lma-root');
        if (container) {
            const root = createRoot(container);
            root.render(
                React.createElement(CallWithChatComposite, {
                    adapter,
                    formFactor: 'desktop',
                    options: { callControls: { displayType: 'compact' } },
                } as any),
            );
        }
        w.__lmaSdkReady = true;
        log('adapter ready');
    } catch (e: any) {
        w.__lmaJoinError = { reason: (e && e.message) || 'adapter creation failed' };
        log(`init error: ${e}`);
    }
}

if (document.readyState === 'complete' || document.readyState === 'interactive') void init();
else document.addEventListener('DOMContentLoaded', () => void init());
