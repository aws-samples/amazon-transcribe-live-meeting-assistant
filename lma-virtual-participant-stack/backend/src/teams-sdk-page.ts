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

// Collect the SDK's peer connections so outbound video RTP can be inspected.
w.__lmaPeerConnections = [];
const NativeRTCPeerConnection = window.RTCPeerConnection;
if (NativeRTCPeerConnection) {
    const Patched = function (this: any, ...args: any[]) {
        const pc = new (NativeRTCPeerConnection as any)(...args);
        try {
            w.__lmaPeerConnections.push(pc);
        } catch { /* noop */ }
        return pc;
    } as unknown as typeof RTCPeerConnection;
    Patched.prototype = NativeRTCPeerConnection.prototype;
    window.RTCPeerConnection = Patched;
}

// The container has no real camera, so ACS's deviceManager.getCameras() returns
// an empty list and the composite's own camera flow can never start. Advertise
// one videoinput device; the Simli getUserMedia override answers the actual
// capture request, keeping the composite's camera state consistent with what is
// published (bypassing it with call.startVideo leaves the two out of sync).
const nativeEnumerateDevices = navigator.mediaDevices?.enumerateDevices?.bind(navigator.mediaDevices);
if (nativeEnumerateDevices) {
    navigator.mediaDevices.enumerateDevices = async () => {
        const devices = await nativeEnumerateDevices();
        if (devices.some((d) => d.kind === 'videoinput')) return devices;
        const virtual = {
            deviceId: 'lma-avatar-camera',
            groupId: 'lma-avatar-camera',
            kind: 'videoinput' as MediaDeviceKind,
            label: 'LMA Avatar Camera',
            toJSON() {
                return this;
            },
        };
        return [...devices, virtual as MediaDeviceInfo];
    };
}

function htmlToText(html: string): string {
    try {
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        return (parsed.body.textContent || '').trim();
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
let rawCameraTrack: MediaStreamTrack | null = null;
const pendingOutgoingChat: string[] = [];
w.__lmaCameraDiag = null;

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
    if (rawCameraStarted) {
        // A live track is the only honest signal; a stale flag would hide the
        // avatar going dark from the watchdog.
        return !!rawCameraTrack && rawCameraTrack.readyState === 'live' && !rawCameraTrack.muted;
    }
    const state = getState();
    return !!(state && state.call && state.call.localVideoStreams && state.call.localVideoStreams.length > 0);
};

w.__lmaStartCamera = async () => {
    try {
        await adapter.startCamera();
        const state = getState();
        if (state && state.call && state.call.localVideoStreams && state.call.localVideoStreams.length > 0) {
            log('camera started via adapter');
            w.__lmaCameraDiag = { path: 'adapter' };
            return true;
        }
    } catch (e) {
        log(`adapter.startCamera failed: ${e}`);
    }
    try {
        if (!call) throw new Error('no call handle');
        const stream = await navigator.mediaDevices.getUserMedia({ video: true });
        const track = stream.getVideoTracks()[0];
        if (!track) throw new Error('getUserMedia returned no video track');
        const settings = typeof track.getSettings === 'function' ? track.getSettings() : {};
        await call.startVideo(new LocalVideoStream(stream));
        rawCameraStarted = true;
        rawCameraTrack = track;
        w.__lmaCameraDiag = {
            path: 'rawMedia',
            trackLabel: track.label,
            readyState: track.readyState,
            muted: track.muted,
            enabled: track.enabled,
            width: (settings as any).width,
            height: (settings as any).height,
            frameRate: (settings as any).frameRate,
        };
        log(`camera started via raw media stream (${JSON.stringify(w.__lmaCameraDiag)})`);
        return true;
    } catch (e) {
        log(`raw media camera failed: ${e}`);
        w.__lmaCameraDiag = { path: 'failed', error: String(e) };
        return false;
    }
};

// Outbound RTP is the ground truth for "are other participants seeing frames":
// a published track with framesEncoded stuck at 0 renders as a blank tile.
w.__lmaVideoSendStats = async () => {
    try {
        const state = getState();
        const streams = (state && state.call && state.call.localVideoStreams) || [];
        const sdkTrack: MediaStreamTrack | null = (() => {
            try {
                const ms = streams[0] && (streams[0].mediaStreamType ? null : streams[0]);
                const raw = rawCameraTrack || (ms && ms.getVideoTracks && ms.getVideoTracks()[0]);
                return raw || rawCameraTrack || null;
            } catch {
                return rawCameraTrack;
            }
        })();
        const out: any = {
            diag: w.__lmaCameraDiag,
            localVideoStreams: streams.length,
            track: sdkTrack
                ? { readyState: sdkTrack.readyState, muted: sdkTrack.muted, enabled: sdkTrack.enabled }
                : null,
            pageHidden: document.hidden,
        };
        const pcs = w.__lmaPeerConnections || [];
        const outbound: any[] = [];
        const sources: any[] = [];
        const senders: any[] = [];
        for (let i = 0; i < pcs.length; i += 1) {
            const pc = pcs[i];
            if (!pc || typeof pc.getStats !== 'function') continue;
            if (pc.connectionState === 'closed') continue;
            try {
                for (const s of pc.getSenders()) {
                    if (!s.track || s.track.kind !== 'video') continue;
                    const params = typeof s.getParameters === 'function' ? s.getParameters() : ({} as any);
                    const enc = (params.encodings && params.encodings[0]) || {};
                    const tr = pc.getTransceivers().find((t: any) => t.sender === s);
                    senders.push({
                        pc: i,
                        trackId: s.track.id.slice(0, 8),
                        readyState: s.track.readyState,
                        enabled: s.track.enabled,
                        muted: s.track.muted,
                        active: enc.active,
                        maxBitrate: enc.maxBitrate,
                        mid: tr && tr.mid,
                        dir: tr && tr.currentDirection,
                    });
                }
            } catch { /* noop */ }
            const stats = await pc.getStats();
            stats.forEach((r: any) => {
                // media-source counts frames the CAPTURE SOURCE produced;
                // outbound-rtp counts frames the ENCODER shipped. Report every
                // sender (even zero-byte ones) so a stalled transceiver can't
                // hide behind an older one that did send.
                if (r.type === 'media-source' && r.kind === 'video') {
                    sources.push({ pc: i, frames: r.frames, fps: r.framesPerSecond, w: r.width, h: r.height });
                }
                if (r.type === 'outbound-rtp' && r.kind === 'video') {
                    outbound.push({
                        pc: i,
                        ssrc: r.ssrc,
                        mid: r.mid,
                        active: r.active,
                        framesEncoded: r.framesEncoded,
                        framesSent: r.framesSent,
                        bytesSent: r.bytesSent,
                        fps: r.framesPerSecond,
                        w: r.frameWidth,
                        h: r.frameHeight,
                        keyFrames: r.keyFramesEncoded,
                        limitedBy: r.qualityLimitationReason,
                        pcState: pc.connectionState,
                        iceState: pc.iceConnectionState,
                    });
                }
            });
        }
        out.mediaSources = sources;
        out.outboundVideo = outbound;
        out.videoSenders = senders;
        return out;
    } catch (e) {
        return { error: String(e) };
    }
};

// Awaited by Node: an un-awaited leaveCall() loses the race with the browser
// closing, leaving the ACS session stuck as a "Leaving..." ghost participant.
w.__lmaLeave = async () => {
    if (!adapter) return 'no-adapter';
    try {
        await adapter.leaveCall();
    } catch (e) {
        log(`leaveCall error: ${e}`);
    }
    for (let i = 0; i < 30; i += 1) {
        const state = getState();
        const callState = state && state.call && state.call.state;
        if (!state || !state.call || callState === 'Disconnected') return 'disconnected';
        await new Promise((r) => setTimeout(r, 200));
    }
    return 'timeout';
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

const JOIN_REJECTIONS: Array<{ pattern: RegExp; reason: string }> = [
    {
        pattern: /AnonymousJoinDisabled|Anonymous Join is disabled/i,
        reason:
            'Anonymous join is disabled for this Teams tenant by policy (subCode 5723). A tenant admin must enable "Anonymous users can join a meeting" in the Teams admin center meeting settings.',
    },
    {
        pattern: /not allowed to create conversation|"subCode":\s*5222/i,
        reason:
            'This meeting is a personal (Teams Free / teams.live.com) meeting (subCode 5222). ACS Teams interop only supports meetings organized by a Microsoft 365 work or school account on teams.microsoft.com.',
    },
];

const origFetch = window.fetch.bind(window);
window.fetch = async (...args: Parameters<typeof fetch>) => {
    const res = await origFetch(...args);
    try {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] as Request).url;
        if (res.status === 403 && /flightproxy|conv/i.test(url)) {
            const body = await res.clone().text();
            const match = JOIN_REJECTIONS.find((r) => r.pattern.test(body));
            if (match) {
                w.__lmaJoinError = { reason: match.reason, detail: body.slice(0, 400) };
                log(`join rejected: ${match.reason}`);
            }
        }
    } catch {
        /* diagnostic only */
    }
    return res;
};

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
    for (const diagEvent of ['diagnosticChanged', 'callDiagnosticChanged']) {
        try {
            adapter.on(diagEvent, (e: any) => {
                try {
                    log(`diagnostic(${diagEvent}): ${JSON.stringify(e).slice(0, 300)}`);
                } catch {
                    /* noop */
                }
            });
            break;
        } catch {
            /* this adapter build doesn't expose this event name */
        }
    }
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

function serializeError(e: any): string {
    try {
        if (e instanceof Error) {
            const props: Record<string, unknown> = { name: e.name, message: e.message, stack: (e.stack || '').split('\n').slice(0, 6).join(' | ') };
            for (const k of Object.getOwnPropertyNames(e)) {
                if (!(k in props)) props[k] = (e as any)[k];
            }
            return JSON.stringify(props);
        }
        return JSON.stringify(e) || String(e);
    } catch {
        return String(e);
    }
}

async function init(): Promise<void> {
    const cfg = w.__lmaTeamsConfig;
    if (!cfg || !cfg.token || !cfg.endpoint || !cfg.acsUserId) {
        w.__lmaJoinError = { reason: '__lmaTeamsConfig missing or incomplete' };
        log('config missing');
        return;
    }
    let step = 'credential';
    try {
        const credential = new AzureCommunicationTokenCredential(cfg.token);
        const locator = cfg.meetingLink
            ? { meetingLink: cfg.meetingLink }
            : { meetingId: cfg.meetingId, passcode: cfg.passcode || undefined };
        step = 'createAdapter';
        adapter = await createAzureCommunicationCallWithChatAdapter({
            endpoint: cfg.endpoint,
            userId: { communicationUserId: cfg.acsUserId },
            displayName: cfg.displayName || 'LMA',
            credential,
            locator: locator as any,
        });
        step = 'listeners';
        setupListeners();

        step = 'render';
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
        const detail = serializeError(e);
        w.__lmaJoinError = {
            reason: `${(e && e.message) || 'adapter creation failed'} [step=${step}]`,
            detail,
            step,
        };
        log(`init error at step=${step}: ${detail}`);
    }
}

if (document.readyState === 'complete' || document.readyState === 'interactive') void init();
else document.addEventListener('DOMContentLoaded', () => void init());
