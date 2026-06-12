/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page, ConsoleMessage } from 'playwright-core';
import jwt from 'jsonwebtoken';
import { details, matchesEndCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from './details.js';
import { transcriptionService } from './scribe.js';
import { voiceAssistant } from './voice-assistant.js';
import { simliAvatar } from './simli-avatar.js';
import { startZoomSdkServer, ZoomSdkServerHandle } from './zoom-sdk-server.js';

const STATUS_CONNECTED = 2;
const STATUS_DISCONNECTED = 3;

const REASON_HOST_ENDED = 1;
const REASON_KICKED = 6;

function buildSignature(clientId: string, clientSecret: string, meetingNumber: string): string {
    const iat = Math.floor(Date.now() / 1000) - 30;
    const exp = iat + 60 * 60 * 2;
    const payload = {
        appKey: clientId,
        sdkKey: clientId,
        mn: meetingNumber,
        role: 0,
        iat,
        exp,
        tokenExp: exp,
    };
    return jwt.sign(payload, clientSecret, { algorithm: 'HS256', header: { alg: 'HS256', typ: 'JWT' } });
}

export default class ZoomSdk {
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};
    private ended = false;
    private server: ZoomSdkServerHandle | null = null;

    constructor() {
        this.endRequested = new Promise<ExitInfo>((resolve) => {
            this.requestEnd = (info: ExitInfo) => {
                if (this.ended) return;
                this.ended = true;
                resolve(info);
            };
        });
    }

    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        for (const message of messages) {
            if (!message) continue;
            try {
                await page.evaluate((m: string) => (window as any).__lmaSendChat(m), message);
                await new Promise((r) => setTimeout(r, 200));
            } catch (err) {
                console.warn('[zoom-sdk] sendChat failed (non-fatal):', err);
            }
        }
    }

    private async clickBySvg(page: Page, svgSelectors: string[], labelRe: string): Promise<boolean> {
        const handle = await page.evaluateHandle(
            (args: { svgSels: string[]; labelSrc: string }) => {
                for (const sel of args.svgSels) {
                    const svg = document.querySelector(sel);
                    if (svg) {
                        const b = svg.closest('button,[role="button"]');
                        if (b) return b;
                    }
                }
                const re = new RegExp(args.labelSrc, 'i');
                const all = document.querySelectorAll('button[aria-label],[role="button"][aria-label]');
                for (const el of Array.from(all)) {
                    if (re.test(el.getAttribute('aria-label') || '')) return el;
                }
                return null;
            },
            { svgSels: svgSelectors, labelSrc: labelRe },
        );
        const el = handle.asElement();
        if (!el) {
            await handle.dispose();
            return false;
        }
        try {
            await el.click({ force: true });
            return true;
        } catch (err) {
            console.warn('[zoom-sdk] click failed:', err);
            return false;
        } finally {
            await handle.dispose();
        }
    }

    // After joining, the SDK lands muted / video-off and sometimes with audio
    // not yet connected (a "Join Audio" prompt). Drive the in-meeting toolbar so
    // the VP actually captures meeting audio (transcription + voice assistant),
    // speaks (agent_mic), and publishes the avatar camera. Trusted Playwright
    // clicks only — synthetic in-page clicks don't satisfy the SDK's media-gesture
    // requirement (same reason the preview Join needs a real click).
    private async setupInMeetingMedia(page: Page): Promise<void> {
        const wantMic = voiceAssistant.isEnabled() || simliAvatar.isConnected();
        const wantVideo = simliAvatar.isConnected();
        const deadline = Date.now() + 20000;
        let audioDone = false;
        let micDone = false;
        let videoDone = !wantVideo;
        let videoClicks = 0;
        while (Date.now() < deadline) {
            const state = await page
                .evaluate(() => (window as any).__lmaMediaState())
                .catch(() => null);
            if (!state) {
                await new Promise((r) => setTimeout(r, 1000));
                continue;
            }

            if (!audioDone && state.needJoinAudio && !state.audioJoined) {
                if (await this.clickBySvg(page, [], 'join audio|connect audio|computer audio')) {
                    console.log('[zoom-sdk] joined computer audio');
                }
                await new Promise((r) => setTimeout(r, 1500));
                continue;
            }
            if (state.audioJoined) audioDone = true;

            if (!micDone && audioDone) {
                if (wantMic && state.muted) {
                    if (await this.clickBySvg(page, ['svg.SvgAudioUnmute'], 'unmute')) {
                        console.log('[zoom-sdk] unmuted microphone (voice assistant / avatar audio)');
                    }
                } else if (!wantMic && !state.muted) {
                    if (await this.clickBySvg(page, ['svg.SvgAudioMute'], 'mute my|^mute$')) {
                        console.log('[zoom-sdk] muted microphone (no voice assistant)');
                    }
                }
                micDone = true;
            }

            if (!videoDone && state.videoOff) {
                videoClicks += 1;
                if (await this.clickBySvg(page, ['svg.SvgVideoOff', 'svg.SvgVideoOffDisallowed'], 'start video|start my video')) {
                    console.log(`[zoom-sdk] started video (avatar camera) attempt ${videoClicks}`);
                }
                await new Promise((r) => setTimeout(r, 2000));
                if (videoClicks >= 3) videoDone = true;
                continue;
            }
            if (!state.videoOff) videoDone = true;

            if (audioDone && micDone && videoDone) {
                console.log(`[zoom-sdk] media setup complete (audio=${audioDone} mic=${micDone} video=${!wantVideo ? 'n/a' : 'on'})`);
                return;
            }
            await new Promise((r) => setTimeout(r, 1000));
        }
        console.log(`[zoom-sdk] media setup finished (audio=${audioDone} mic=${micDone} videoDone=${videoDone})`);
    }

    // Zoom can flip the camera off mid-meeting; re-start it when the avatar is
    // connected but video reads off. Mirrors zoom.ts startCameraWatchdog.
    private startCameraWatchdog(page: Page): void {
        if (!simliAvatar.isSimliEnabled()) return;
        let consecutiveOff = 0;
        const timer = setInterval(async () => {
            if (this.ended || page.isClosed()) {
                clearInterval(timer);
                return;
            }
            if (!simliAvatar.isConnected()) return;
            const state = await page.evaluate(() => (window as any).__lmaMediaState()).catch(() => null);
            if (state && state.videoOff) {
                consecutiveOff += 1;
                if (consecutiveOff >= 2) {
                    console.warn('[zoom-sdk] camera OFF (x2) with avatar connected — re-starting video');
                    await this.clickBySvg(page, ['svg.SvgVideoOff', 'svg.SvgVideoOffDisallowed'], 'start video|start my video');
                    consecutiveOff = 0;
                }
            } else {
                consecutiveOff = 0;
            }
        }, 10_000);
    }

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        const prepareAvatar = opts.prepareAvatar ?? (async () => {});

        page.on('console', (message: ConsoleMessage) => {
            const text = message.text();
            if (text.includes('[LMA-ZoomSDK]')) console.log(`ZoomSDK page: ${text}`);
        });
        page.on('pageerror', (error: unknown) => console.warn('[zoom-sdk] page error:', error));
        page.on('crash', () => {
            console.error('[zoom-sdk] page crashed (renderer crash) — ending meeting');
            this.requestEnd({ reason: 'page-closed', trigger: 'renderer-crash' });
        });

        const substep = async (message: string): Promise<void> => {
            if (!details.invite.virtualParticipantId) return;
            try {
                const { createStatusManager } = await import('./status-manager.js');
                await createStatusManager(details.invite.virtualParticipantId).setJoiningSubstep(message);
            } catch { /* noop */ }
        };

        const clientId = (process.env.ZOOM_MEETING_SDK_CLIENT_ID || '').trim();
        const clientSecret = (process.env.ZOOM_MEETING_SDK_CLIENT_SECRET || '').trim();
        if (!clientId || !clientSecret) {
            throw new Error('Zoom SDK method selected but ZOOM_MEETING_SDK_CLIENT_ID / ZOOM_MEETING_SDK_CLIENT_SECRET are not set');
        }
        const meetingNumber = (details.invite.meetingId || '').replace(/\D/g, '');
        if (!meetingNumber) {
            throw new Error('meeting not found: meeting ID is empty or non-numeric');
        }
        await substep('Authorizing with Zoom SDK…');
        const signature = buildSignature(clientId, clientSecret, meetingNumber);

        await prepareAvatar();

        this.server = await startZoomSdkServer();
        await page.addInitScript(
            (cfg: any) => {
                (window as any).__lmaZoomConfig = cfg;
            },
            {
                meetingNumber,
                signature,
                passWord: details.invite.meetingPassword || '',
                userName: details.scribeIdentity,
                leaveUrl: `${this.server.origin}/left.html`,
            },
        );
        await substep('Loading Zoom SDK…');
        await page.goto(`${this.server.origin}/`, { waitUntil: 'domcontentloaded' });

        await page.waitForFunction(() => (window as any).__lmaSdkReady === true, undefined, { timeout: 60_000 });

        const isolated = await page.evaluate(() => (window as any).crossOriginIsolated === true).catch(() => false);
        if (!isolated) {
            console.warn('[zoom-sdk] crossOriginIsolated=false — gallery/SAB view may be degraded');
        }

        await substep('Joining the meeting…');
        await page.evaluate(() => (window as any).__lmaZoomJoin());

        const joinDeadline = Date.now() + details.waitingTimeout;
        let joined = false;
        let previewClicked = false;
        while (Date.now() < joinDeadline) {
            if (page.isClosed()) break;

            if (!previewClicked) {
                const btn = await page.$('button.preview-join-button');
                if (btn) {
                    try {
                        await btn.click();
                        previewClicked = true;
                        console.log('[zoom-sdk] clicked preview Join');
                    } catch (err) {
                        console.warn('[zoom-sdk] preview Join click failed (will retry):', err);
                    }
                }
            }

            const state = await page
                .evaluate(() => ({
                    status: (window as any).__lmaMeetingStatus,
                    error: (window as any).__lmaJoinError,
                    waiting: (window as any).__lmaInWaitingRoom,
                    previewActive: typeof (window as any).__lmaPreviewActive === 'function' ? (window as any).__lmaPreviewActive() : false,
                }))
                .catch(() => null);
            if (!state) break;
            if (state.error) {
                const reason = String(state.error.reason || 'join failed');
                throw new Error(`Zoom SDK join failed (code=${state.error.errorCode}): ${reason}`);
            }
            if (state.status === STATUS_CONNECTED && !state.waiting && !state.previewActive) {
                joined = true;
                break;
            }
            await new Promise((r) => setTimeout(r, 1000));
        }

        if (!joined) {
            console.log('[zoom-sdk] never admitted before waitingTimeout');
            return { reason: 'never-joined', trigger: 'sdk-not-admitted' };
        }
        console.log('[zoom-sdk] joined meeting');

        await substep('Setting up audio and video…');
        await this.setupInMeetingMedia(page);
        this.startCameraWatchdog(page);

        await substep('In the meeting — posting introduction…');
        await this.sendMessages(page, details.introMessages);
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(page, details.startMessages);
            transcriptionService.startTranscription();
        }

        let lastSpeaker = '';
        let consecutiveLonely = 0;
        const POLLS_BEFORE_LONELY = 2;
        let pollsSinceLonelyCheck = 0;
        const monitorTimer = setInterval(async () => {
            if (this.ended || page.isClosed()) return;
            let snap: { speaker: string; chats: Array<{ senderName: string; text: string }>; count: number; domCount: number; status: number; selfLeave: number | null } | null;
            try {
                snap = await page.evaluate(() => {
                    const w = window as any;
                    const chats = w.__lmaChatQueue.splice(0, w.__lmaChatQueue.length);
                    return {
                        speaker: w.__lmaActiveSpeaker || '',
                        chats,
                        count: w.__lmaParticipantCount,
                        domCount: typeof w.__lmaParticipantCountDom === 'function' ? w.__lmaParticipantCountDom() : -1,
                        status: w.__lmaMeetingStatus,
                        selfLeave: w.__lmaSelfLeaveReason,
                    };
                });
            } catch {
                return;
            }
            if (!snap) return;

            if (snap.status === STATUS_DISCONNECTED) {
                const removed = snap.selfLeave === REASON_KICKED;
                const hostEnded = snap.selfLeave === REASON_HOST_ENDED;
                this.requestEnd({
                    reason: removed ? 'removed-from-meeting' : 'host-ended',
                    trigger: hostEnded ? 'SDK_HOST_ENDED' : removed ? 'SDK_REMOVED' : 'SDK_DISCONNECTED',
                });
                return;
            }

            if (snap.speaker && snap.speaker !== lastSpeaker) {
                lastSpeaker = snap.speaker;
                if (snap.speaker !== details.scribeIdentity && snap.speaker !== details.scribeName) {
                    await transcriptionService.speakerChange(snap.speaker).catch(() => {});
                }
            }

            for (const chat of snap.chats) {
                const sender = chat.senderName || null;
                const body = chat.text || '';
                if (matchesEndCommand(body)) {
                    console.log(`[zoom-sdk] asked to leave by ${sender || 'a participant'}: ${JSON.stringify(body)}`);
                    await this.sendMessages(page, exitMessagesFor(sender));
                    details.start = false;
                    this.requestEnd({ reason: 'end-command', trigger: 'chat', requestedBy: sender, matchedMessage: body });
                    return;
                }
                if (details.start && body.includes(details.pauseCommand)) {
                    details.start = false;
                    console.log(details.pauseMessages[0]);
                    await this.sendMessages(page, details.pauseMessages);
                } else if (!details.start && body.includes(details.startCommand)) {
                    details.start = true;
                    console.log(details.startMessages[0]);
                    await this.sendMessages(page, details.startMessages);
                    transcriptionService.startTranscription();
                } else if (details.start) {
                    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
                    const formatted = `[${timestamp}] ${sender ? `${sender}: ` : ''}${body}`;
                    details.messages.push(formatted);
                    console.log('New message:', formatted);
                }
            }

            pollsSinceLonelyCheck += 1;
            if (pollsSinceLonelyCheck >= 10) {
                pollsSinceLonelyCheck = 0;
                // Prefer the in-meeting toolbar counter (authoritative); fall
                // back to the event-accumulated count only when the DOM counter
                // is absent (domCount === -1), so a missed onUserJoin can't
                // falsely strand the VP as "alone".
                const effectiveCount = snap.domCount >= 0 ? snap.domCount : snap.count;
                if (effectiveCount <= 1) {
                    consecutiveLonely += 1;
                    if (consecutiveLonely >= POLLS_BEFORE_LONELY) {
                        console.log(`[zoom-sdk] alone in meeting (count=${effectiveCount}) — leaving`);
                        details.start = false;
                        this.requestEnd({ reason: 'alone-in-meeting', trigger: 'sdk-participants' });
                    }
                } else {
                    consecutiveLonely = 0;
                }
            }
        }, 3000);

        console.log('Waiting for meeting end.');
        const timeoutPromise = new Promise<ExitInfo>((resolve) =>
            setTimeout(() => resolve({ reason: 'meeting-timeout', trigger: 'meetingTimeout' }), details.meetingTimeout),
        );
        const exitInfo = await Promise.race([this.endRequested, timeoutPromise]);
        clearInterval(monitorTimer);
        details.start = false;

        try {
            if (!page.isClosed()) await page.evaluate(() => (window as any).__lmaLeave());
        } catch { /* noop */ }
        try {
            await this.server?.close();
        } catch { /* noop */ }

        console.log(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
        return exitInfo;
    }
}
