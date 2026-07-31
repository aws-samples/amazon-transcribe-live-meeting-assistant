/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page, ConsoleMessage } from 'playwright-core';
import { CommunicationIdentityClient } from '@azure/communication-identity';
import { details, matchesEndCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from './details.js';
import { transcriptionService } from './scribe.js';
import { voiceAssistant } from './voice-assistant.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { simliAvatar } from './simli-avatar.js';
import { startTeamsSdkServer, TeamsSdkServerHandle } from './teams-sdk-server.js';

export function endpointFromConnectionString(connectionString: string): string {
    const match = /endpoint=([^;]+)/i.exec(connectionString);
    if (!match) throw new Error('ACS_CONNECTION_STRING is missing an endpoint= segment');
    return match[1].replace(/\/+$/, '');
}

// The invitation parser stores a numeric id + separate passcode for new-style
// invites, but keeps the full URL as meetingId for older meetup-join links, and
// the UI lets users paste a URL directly. ACS takes either a meetingId+passcode
// or a meetingLink, so classify the three shapes here.
export function resolveTeamsLocator(
    rawMeetingId: string,
    meetingPassword?: string,
): { meetingId: string; meetingLink: string } {
    const trimmed = rawMeetingId.trim();
    if (!trimmed) return { meetingId: '', meetingLink: '' };
    if (/^https?:\/\//i.test(trimmed)) {
        return { meetingId: '', meetingLink: trimmed.replace(/\s/g, '') };
    }
    const bare = trimmed.replace(/\s/g, '').replace(/\?.*$/, '');
    if (!bare) return { meetingId: '', meetingLink: '' };
    if (/^\d+$/.test(bare)) return { meetingId: bare, meetingLink: '' };
    const passcode = meetingPassword ? `?p=${encodeURIComponent(meetingPassword)}` : '';
    return { meetingId: '', meetingLink: `https://teams.microsoft.com/meet/${bare}${passcode}` };
}

export default class TeamsSdk {
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};
    private ended = false;
    private server: TeamsSdkServerHandle | null = null;
    private acsUser: { communicationUserId: string } | null = null;
    private identityClient: CommunicationIdentityClient | null = null;
    private lastMeetingSpeaker: string | null = null;
    private lastReportedSpeaker: string | null = null;
    private detachAgentSpeaking: (() => void) | null = null;

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
                console.warn('[teams-sdk] sendChat failed (non-fatal):', err);
            }
        }
    }

    private async setupInMeetingMedia(page: Page): Promise<void> {
        const wantMic = voiceAssistant.isEnabled() || simliAvatar.isConnected();
        const wantVideo = simliAvatar.isConnected();
        try {
            await page.evaluate((m: boolean) => (window as any).__lmaSetMuted(m), !wantMic);
            console.log(`[teams-sdk] microphone ${wantMic ? 'unmuted (voice assistant / avatar audio)' : 'muted (no voice assistant)'}`);
        } catch (err) {
            console.warn('[teams-sdk] mute/unmute failed:', err);
        }
        if (!wantVideo) return;
        for (let attempt = 1; attempt <= 3; attempt += 1) {
            const ok = await page
                .evaluate(() => (window as any).__lmaStartCamera())
                .catch(() => false);
            if (ok) {
                const diag = await page.evaluate(() => (window as any).__lmaCameraDiag).catch(() => null);
                console.log(
                    `[teams-sdk] started video (avatar camera) attempt ${attempt} ${diag ? JSON.stringify(diag) : ''}`,
                );
                this.startVideoSendMonitor(page);
                return;
            }
            await new Promise((r) => setTimeout(r, 2000));
        }
        console.warn('[teams-sdk] failed to start avatar camera after 3 attempts');
    }

    // Frames actually leaving the container is the only proof other attendees
    // see the avatar; a published-but-empty track renders as a blank tile.
    private startVideoSendMonitor(page: Page): void {
        let reports = 0;
        const timer = setInterval(async () => {
            if (this.ended || page.isClosed() || reports >= 3) {
                clearInterval(timer);
                return;
            }
            reports += 1;
            const stats = await page.evaluate(() => (window as any).__lmaVideoSendStats()).catch(() => null);
            if (!stats) return;
            const sent = (stats.outboundVideo || []).reduce(
                (n: number, r: any) => n + (r.framesSent || r.framesEncoded || 0),
                0,
            );
            console.log(`[teams-sdk] video send stats: ${JSON.stringify(stats)}`);
            if (sent === 0) {
                console.warn(
                    '[teams-sdk] no video frames sent yet — remote participants will see a blank tile',
                );
            }
        }, 10_000);
    }

    // Speaker attribution combines two signals: ACS dominant-speaker events
    // (who the meeting says is talking) and AgentSpeakingDetector (whether the
    // voice agent / avatar is talking, measured on agent_output.monitor).
    // Without the second signal the agent's speech keeps the last human's
    // label. Mirrors the DOM Teams path.
    private async reportSpeaker(speaker: string | null): Promise<void> {
        if (!speaker || speaker === this.lastReportedSpeaker) return;
        this.lastReportedSpeaker = speaker;
        await transcriptionService.speakerChange(speaker).catch(() => {});
    }

    private startAgentSpeakingAttribution(): void {
        if (!voiceAssistant.isEnabled()) return;
        const onAgentStart = () => {
            this.reportSpeaker(details.scribeIdentity).catch(() => {});
        };
        const onAgentStop = () => {
            this.reportSpeaker(this.lastMeetingSpeaker).catch(() => {});
        };
        agentSpeakingDetector.on('started', onAgentStart);
        agentSpeakingDetector.on('stopped', onAgentStop);
        if (agentSpeakingDetector.isSpeaking()) onAgentStart();
        this.detachAgentSpeaking = () => {
            agentSpeakingDetector.off('started', onAgentStart);
            agentSpeakingDetector.off('stopped', onAgentStop);
        };
    }

    private async leaveCall(page: Page): Promise<void> {
        if (page.isClosed()) return;
        try {
            const result = await Promise.race([
                page.evaluate(() => (window as any).__lmaLeave?.()),
                new Promise((r) => setTimeout(() => r('leave-timeout'), 8000)),
            ]);
            console.log(`[teams-sdk] left call (${result})`);
        } catch (err) {
            console.warn('[teams-sdk] leave failed (session may linger in the roster):', err);
        }
    }

    private startCameraWatchdog(page: Page): void {
        if (!simliAvatar.isSimliEnabled()) return;
        let consecutiveOff = 0;
        const timer = setInterval(async () => {
            if (this.ended || page.isClosed()) {
                clearInterval(timer);
                return;
            }
            if (!simliAvatar.isConnected()) return;
            const on = await page.evaluate(() => (window as any).__lmaCameraOn()).catch(() => true);
            if (!on) {
                consecutiveOff += 1;
                if (consecutiveOff >= 2) {
                    console.warn('[teams-sdk] camera OFF (x2) with avatar connected — re-starting video');
                    await page.evaluate(() => (window as any).__lmaStartCamera()).catch(() => {});
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
            if (text.includes('[LMA-TeamsSDK]')) {
                console.log(`TeamsSDK page: ${text}`);
            } else if (message.type() === 'error' || message.type() === 'warning') {
                console.log(`TeamsSDK page ${message.type()}: ${text.slice(0, 500)}`);
            }
        });
        // Response bodies can carry tokens/identity material, so the body dump
        // is opt-in; the join-rejection reasons we need are mapped in-page.
        const logErrorBodies = (process.env.TEAMS_SDK_DEBUG_HTTP || '').toLowerCase() === 'true';
        page.on('response', (response) => {
            const status = response.status();
            if (status >= 400) {
                console.log(`[teams-sdk] HTTP ${status} ← ${response.url().slice(0, 300)}`);
                if (logErrorBodies) {
                    response
                        .text()
                        .then((body) => {
                            if (body) console.log(`[teams-sdk] HTTP ${status} body: ${body.slice(0, 600)}`);
                        })
                        .catch(() => {});
                }
            }
        });
        page.on('requestfailed', (request) => {
            console.log(
                `[teams-sdk] request FAILED (${request.failure()?.errorText ?? 'unknown'}) ← ${request.url().slice(0, 300)}`,
            );
        });
        page.on('pageerror', (error: unknown) => console.warn('[teams-sdk] page error:', error));
        page.on('crash', () => {
            console.error('[teams-sdk] page crashed (renderer crash) — ending meeting');
            this.requestEnd({ reason: 'page-closed', trigger: 'renderer-crash' });
        });

        const substep = async (message: string): Promise<void> => {
            if (!details.invite.virtualParticipantId) return;
            try {
                const { createStatusManager } = await import('./status-manager.js');
                await createStatusManager(details.invite.virtualParticipantId).setJoiningSubstep(message);
            } catch { /* noop */ }
        };

        const connectionString = (process.env.ACS_CONNECTION_STRING || '').trim();
        if (!connectionString) {
            throw new Error('Teams SDK method selected but ACS_CONNECTION_STRING is not set');
        }
        const endpoint = endpointFromConnectionString(connectionString);
        const { meetingId, meetingLink } = resolveTeamsLocator(
            details.invite.meetingId || '',
            details.invite.meetingPassword,
        );
        if (!meetingId && !meetingLink) {
            throw new Error('meeting not found: meeting ID is empty');
        }
        // Redact the query string: the passcode rides in ?p= and this goes to CloudWatch.
        if (meetingLink) console.log(`[teams-sdk] joining via meeting link ${meetingLink.split('?')[0]}`);

        await substep('Authorizing with Azure Communication Services…');
        const identityClient = new CommunicationIdentityClient(connectionString);
        // Scope the token to this meeting instead of the 24h default (ACS allows
        // 60..1440 minutes), and delete the identity on exit so tokens can't
        // outlive the meeting and identities don't accumulate on the resource.
        const tokenMinutes = Math.min(1440, Math.max(60, Math.ceil(details.meetingTimeout / 60_000) + 30));
        const { user, token } = await identityClient.createUserAndToken(['voip', 'chat'], {
            tokenExpiresInMinutes: tokenMinutes,
        });
        this.acsUser = user;
        this.identityClient = identityClient;

        await prepareAvatar();

        this.server = await startTeamsSdkServer();
        try {
            await page.addInitScript(
                (cfg: any) => {
                    (window as any).__lmaTeamsConfig = cfg;
                },
                {
                    token,
                    endpoint,
                    acsUserId: user.communicationUserId,
                    displayName: details.scribeIdentity,
                    meetingId,
                    meetingLink,
                    passcode: details.invite.meetingPassword || '',
                    wantMic: voiceAssistant.isEnabled() || simliAvatar.isConnected(),
                },
            );
            await substep('Loading Teams meeting client…');
            await page.goto(`${this.server.origin}/`, { waitUntil: 'domcontentloaded' });

            await page.waitForFunction(
                () => (window as any).__lmaSdkReady === true || (window as any).__lmaJoinError !== null,
                undefined,
                { timeout: 60_000 },
            );
            const initError = await page.evaluate(() => (window as any).__lmaJoinError).catch(() => null);
            if (initError) {
                if (initError.detail) console.error(`[teams-sdk] init error detail: ${initError.detail}`);
                throw new Error(`Teams SDK setup failed: ${initError.reason || 'unknown'}`);
            }

            await substep('Joining the meeting…');
            await page.evaluate(() => (window as any).__lmaTeamsJoin());

            const joinDeadline = Date.now() + details.waitingTimeout;
            let joined = false;
            let lobbyAnnounced = false;
            while (Date.now() < joinDeadline) {
                if (page.isClosed()) break;
                const state = await page
                    .evaluate(() => ({
                        call: (window as any).__lmaCallState(),
                        error: (window as any).__lmaJoinError,
                        endReason: (window as any).__lmaCallEndReason,
                    }))
                    .catch(() => null);
                if (!state) break;
                if (state.error) {
                    throw new Error(`Teams SDK join failed: ${state.error.reason || 'join failed'}`);
                }
                if (state.call.page === 'accessDeniedTeamsMeeting') {
                    // Avoid the words "password"/"passcode": index.ts classifies
                    // failure messages by substring and would report this as
                    // "Wrong meeting password", hiding the real diagnosis.
                    throw new Error('Teams SDK join failed: access denied (the tenant may block anonymous joins, or the meeting ID / entry code is wrong)');
                }
                if (state.call.callState === 'InLobby' || state.call.page === 'lobby') {
                    if (!lobbyAnnounced) {
                        lobbyAnnounced = true;
                        console.log('[teams-sdk] in lobby, waiting to be admitted');
                        await substep('Waiting to be admitted from the lobby…');
                    }
                }
                if (state.endReason) {
                    console.log(`[teams-sdk] call ended before admission (code=${state.endReason.code} subCode=${state.endReason.subCode})`);
                    return { reason: 'never-joined', trigger: 'acs-not-admitted' };
                }
                if (state.call.callState === 'Connected' && state.call.page === 'call') {
                    joined = true;
                    break;
                }
                await new Promise((r) => setTimeout(r, 1000));
            }

            if (!joined) {
                console.log('[teams-sdk] never admitted before waitingTimeout');
                return { reason: 'never-joined', trigger: 'acs-not-admitted' };
            }
            console.log('[teams-sdk] joined meeting');

            await substep('Setting up audio and video…');
            await this.setupInMeetingMedia(page);
            this.startCameraWatchdog(page);
            this.startAgentSpeakingAttribution();

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
                let snap: {
                    speaker: string;
                    chats: Array<{ senderName: string; text: string }>;
                    count: number;
                    call: { page: string; callState: string };
                    endReason: { code?: number; subCode?: number } | null;
                } | null;
                try {
                    snap = await page.evaluate(() => {
                        const w = window as any;
                        const chats = w.__lmaChatQueue.splice(0, w.__lmaChatQueue.length);
                        return {
                            speaker: w.__lmaActiveSpeaker || '',
                            chats,
                            count: w.__lmaParticipantCount(),
                            call: w.__lmaCallState(),
                            endReason: w.__lmaCallEndReason,
                        };
                    });
                } catch {
                    return;
                }
                if (!snap) return;

                if (snap.call.callState === 'Disconnected' || snap.endReason) {
                    const removed = snap.call.page === 'removedFromCall';
                    const codes = snap.endReason
                        ? `code=${snap.endReason.code} subCode=${snap.endReason.subCode}`
                        : 'no endReason';
                    console.log(`[teams-sdk] call ended (page=${snap.call.page} ${codes})`);
                    this.requestEnd({
                        reason: removed ? 'removed-from-meeting' : 'host-ended',
                        trigger: `ACS_${removed ? 'REMOVED' : 'DISCONNECTED'} ${codes}`,
                    });
                    return;
                }

                if (snap.speaker && snap.speaker !== lastSpeaker) {
                    lastSpeaker = snap.speaker;
                    if (snap.speaker !== details.scribeIdentity && snap.speaker !== details.scribeName) {
                        this.lastMeetingSpeaker = snap.speaker;
                        // Don't overwrite the agent's label mid-utterance; the
                        // detector's 'stopped' handler restores this speaker.
                        if (!agentSpeakingDetector.isSpeaking()) {
                            await this.reportSpeaker(snap.speaker);
                        }
                    }
                }

                for (const chat of snap.chats) {
                    const sender = chat.senderName || null;
                    const body = chat.text || '';
                    if (matchesEndCommand(body)) {
                        console.log(`[teams-sdk] asked to leave by ${sender || 'a participant'}: ${JSON.stringify(body)}`);
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
                    if (snap.count >= 0 && snap.count <= 1) {
                        consecutiveLonely += 1;
                        if (consecutiveLonely >= POLLS_BEFORE_LONELY) {
                            console.log(`[teams-sdk] alone in meeting (count=${snap.count}) — leaving`);
                            details.start = false;
                            this.requestEnd({ reason: 'alone-in-meeting', trigger: 'acs-participants' });
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

            console.log(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
            return exitInfo;
        } finally {
            // Must run on every exit path, not just a normal meeting end: the
            // lobby-timeout returns and the join throws would otherwise abandon
            // the ACS session, leaving a "Leaving..." ghost in the roster.
            this.detachAgentSpeaking?.();
            this.detachAgentSpeaking = null;
            await this.leaveCall(page);
            try {
                await this.server?.close();
            } catch { /* noop */ }
            if (this.identityClient && this.acsUser) {
                try {
                    await this.identityClient.deleteUser(this.acsUser);
                    console.log('[teams-sdk] revoked ACS identity');
                } catch (err) {
                    console.warn('[teams-sdk] failed to delete ACS identity (token expires on its own):', err);
                }
            }
        }
    }
}
