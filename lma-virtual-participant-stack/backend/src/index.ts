import { launchPersistentContext } from 'cloakbrowser';
import { promises as fs, readFileSync } from 'fs';
import Chime from './chime.js';
import Zoom from './zoom.js';
import ZoomSdk from './zoom-sdk.js';
import Teams from './teams.js';
import TeamsSdk from './teams-sdk.js';
import {
    AcsJoinRefusedError,
    acsFallbackLogLine,
    acsFallbackSubstep,
    noteAcsRefusal,
    teamsSdkFallbackAllowed,
} from './teams-join-method.js';
import Webex from './webex.js';
import GoogleMeet from './google-meet.js';
import { grantMeetingMediaPermissions } from './meeting-permissions.js';
import { blankCameraInitScript } from './blank-camera.js';
import { details, ExitInfo, formatExitMessage, didJoinMeeting, normalizeExitTrigger } from './details.js';
import { startupMeetingPassword } from './startup-meeting-password.js';
import { failLaunchIfPasswordUnreadable } from './meeting-password-source.js';
import { transcriptionService } from './scribe.js';
import { VirtualParticipantStatusManager, beginShutdown } from './status-manager.js';
import { readCallSession, type VpCallSession } from './call-session.js';
import { recordingService } from './recording.js';
import { videoRecorder } from './video-recorder.js';
import { keyframeSampler } from './keyframe-sampler.js';
import { screenShareProbeFor } from './screen-share-probe.js';
import {
    installAudioDiagnostics,
    startAudioDeviceSpecPolling,
    startAudioDiagnosticsPolling,
    startSenderTrackBypass,
} from './audio-diagnostics.js';
import { sendEndMeeting, sendStartMeeting } from './kinesis-stream.js';
import { MCPCommandHandler } from './mcp-command-handler.js';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { simliAvatar } from './simli-avatar.js';
import { voiceAssistant } from './voice-assistant.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { acquireProfile, persistProfile, releaseProfile } from './profile-store.js';
import { requiresAlbSelfRegistration } from './launch-mode.js';
import {
    CONTEXT_VIRTUAL_PARTICIPANT,
    LaunchType,
    currentCallId,
    recordMetering,
    resolveComputeSize,
    virtualParticipantUsage,
} from './metering.js';
import {
    patchPreferencesFor3pCookies,
    patchPreferencesForExternalProtocols,
    profileIsFresh,
    initProfileDefaults,
    cleanStaleLocks,
    warmupNavigation,
    fingerprintSeedForUser,
    stableFingerprintSeed,
} from './lib/profile.js';
import { bindLogContext, createLogger, logMeetingPlatform } from './logger.js';
import { meetingConsent, prepareMeetingConsent } from './consent-runtime.js';
import { leaveWithinBudget, shouldLeaveAfterExit } from './meeting-leave.js';
import { MeetingTeardown } from './meeting-teardown.js';
import { noteMeetingOver } from './meeting-phase.js';

const log = createLogger('main');

// Match the Xvfb screen size; fluxbox toolbar is suppressed in entrypoint.sh.
const WINDOW_WIDTH = 1920;
const WINDOW_HEIGHT = 1080 - 130;

const isTeamsPlatform = (platform: string): boolean => platform.toUpperCase() === 'TEAMS';

// The method chosen at start-up. Read before the join for the browser launch
// flags and the warmup decision, which cannot change afterwards; when Teams
// refuses the ACS join, details.teamsMethod becomes 'dom' (see the join below)
// and the consumers that run after that point read it again.
const isTeamsSdkJoin = (): boolean =>
    isTeamsPlatform(details.invite.meetingPlatform) && details.teamsMethod === 'sdk';

const getCloakLaunchArgs = (fingerprintSeed: number, needWebrtcHostCandidates: boolean): string[] => [
    `--fingerprint=${fingerprintSeed}`,
    `--fingerprint-screen-width=${WINDOW_WIDTH}`,
    `--fingerprint-screen-height=${WINDOW_HEIGHT}`,
    // Size the headed OS window to fill the Xvfb display.
    `--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}`,
    '--window-position=0,0',
    '--use-fake-ui-for-media-stream',
    '--autoplay-policy=no-user-gesture-required',
    '--disable-blink-features=AutomationControlled',
    '--disable-notifications',
    '--disable-extensions',
    '--disable-crash-reporter',
    '--disable-dev-shm-usage',
    '--enable-logging',
    '--v=1',
    '--enable-logging=stderr',
    '--log-level=0',
    '--remote-debugging-port=9222',
    // PERF: disable GPU entirely. There is no hardware GPU in the container, so
    // Chrome runs all WebGL/compositing on the CPU via SwiftShader (the GPU
    // process measured ~200% under load — the dominant cost). cloakbrowser
    // force-adds --ignore-gpu-blocklist in headed mode to make WebGL work in
    // Docker/Xvfb, but its arg-merge lets our args override by key. We don't
    // need to SEE Zoom's video (we inject our own camera and consume audio), so
    // disabling GPU/WebGL sheds the SwiftShader cost.
    '--disable-gpu',
    '--disable-software-rasterizer',
    '--disable-infobars',
    '--test-type',
    // Suppress password/autofill bubbles that overlay meeting UI buttons.
    '--disable-features=PasswordManagerOnboarding,AutofillEnableAccountWalletStorage,PasswordImport,PasswordsAccountStorage,Translate',
    '--password-store=basic',
    '--use-mock-keychain',
    '--no-first-run',
    '--no-default-browser-check',
    // CloakBrowser's compiled WebRTC IP-leak patch suppresses ALL ICE host
    // candidates by default, which prevents any in-page WebRTC media from
    // connecting: the Simli avatar's page-to-page video bridge, and the ACS
    // calling SDK's media transport for the Teams SDK join path (the call
    // signals fine but media setup fails and ACS drops the call with
    // code=490 subCode=4502 while still in the lobby). Both flags are
    // required to restore host candidate gathering; verified empirically.
    // Only the container's private RFC1918 IP is exposed, and no proxy is in
    // use, so this leaks nothing the TCP connection doesn't already reveal.
    ...(needWebrtcHostCandidates ? ['--force-webrtc-ip-handling-policy=default'] : []),
    ...(needWebrtcHostCandidates ? ['--webrtc-ip-handling-policy=default'] : []),
];

// Global variables for graceful shutdown
let shutdownRequested = false;
let cleanupInProgress = false;
let statusManager: VirtualParticipantStatusManager | null = null;
let vpId: string | null = null;
let mcpHandler: MCPCommandHandler | null = null;
let strandsWarmupTimer: NodeJS.Timeout | null = null;
// Hoisted to module scope so the signal/emergency shutdown paths can close the
// browser and flush the profile to S3 — not just main()'s normal cleanup.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let browserContext: any = null;
let activeProfileHandle: import('./profile-store.js').ProfileHandle | null = null;
let profilePersisted = false;

// The platform handler's leave for the meeting being joined, set once main()
// has chosen the handler, so the signal path can leave the meeting before it
// closes the browser (meeting-leave.ts).
let leaveActiveMeeting: (() => Promise<boolean>) | null = null;

// Leave the meeting on the platform within the leave budget. Never throws.
const leaveMeetingBeforeClose = async (why: string): Promise<void> => {
    const leave = leaveActiveMeeting;
    if (!leave) return;
    leaveActiveMeeting = null;
    log.always(`Leaving the meeting (${why})...`);
    const outcome = await leaveWithinBudget(leave);
    log.always(`Leave the meeting: ${outcome}`);
};

// Stops the page's polling timers; set in main() once they are running. An
// un-cleared interval holding a page reference would keep the process alive.
let stopPageMonitors: () => void = () => {};

// The end-of-meeting work, run once by whichever shutdown path gets there
// first: main()'s cleanup or the stop-signal handler (meeting-teardown.ts).
const meetingTeardown = new MeetingTeardown({
    stopPageMonitors: () => stopPageMonitors(),
    stopConsent: () => meetingConsent.stop(),
    consentEvidenceSettled: () => meetingConsent.evidenceSettled(),
    consentVideoSettled: () => meetingConsent.videoSettled(),
    leaveMeeting: (why) => leaveMeetingBeforeClose(why),
    stopKeyframes: () => keyframeSampler.stop(),
    stopTranscription: () => transcriptionService.stopTranscription(),
    finishAudioRecording: () => recordingService.cleanup(),
    // Separate recording URL event (matching Python)
    sendRecordingUrl: async (url) => {
        const { kinesisStreamManager } = await import('./kinesis-stream.js');
        await kinesisStreamManager.sendCallRecording(url);
    },
    finishVideoRecording: () => videoRecorder.stop(),
    sendVideoRecordingUrl: async (url) => {
        const { kinesisStreamManager } = await import('./kinesis-stream.js');
        await kinesisStreamManager.sendCallVideoRecording(url);
    },
    sendEnd: () => sendEndMeeting(),
});

// Stop the services that outlive the meeting page: the Strands warmup timer,
// the MCP handler, the Simli avatar and the agent speaking detector. Shared
// by both shutdown paths. Never throws.
const releaseServices = async (): Promise<void> => {
    if (strandsWarmupTimer) {
        clearInterval(strandsWarmupTimer);
        strandsWarmupTimer = null;
        log.info('✓ Strands warmup timer stopped');
    }
    if (mcpHandler) {
        try {
            await mcpHandler.stop();
            log.info('✓ MCP handler stopped');
        } catch (error) {
            log.error('Error stopping MCP handler:', error);
        }
    }
    try {
        await simliAvatar.stop();
        log.info('✓ Simli Avatar stopped');
    } catch (error) {
        log.error('Error stopping Simli Avatar:', error);
    }
    try {
        agentSpeakingDetector.stop();
    } catch (error) {
        log.error('Error stopping AgentSpeakingDetector:', error);
    }
};

// When main() began: the start of this Virtual Participant's own compute as the
// meeting cost estimate counts it. Set in main() rather than at module load so
// a MicroVM resumed from a snapshot is not charged for the time it sat in it.
let runStartedMs = Date.now();
let runtimeMetered = false;

// Record this Virtual Participant's compute time against its meeting, once,
// from whichever shutdown path runs first. Never throws.
const meterRuntime = async (): Promise<void> => {
    if (runtimeMetered) {
        return;
    }
    runtimeMetered = true;
    try {
        const callId = currentCallId();
        if (!callId) {
            return;
        }
        const launchType = (process.env.VP_LAUNCH_TYPE || 'FARGATE').toUpperCase() as LaunchType;
        const size = await resolveComputeSize(launchType);
        const runSeconds = (Date.now() - runStartedMs) / 1000;
        const { service, usage } = virtualParticipantUsage(launchType, runSeconds, size);
        await recordMetering(callId, CONTEXT_VIRTUAL_PARTICIPANT, service, usage, {
            launchType,
            vcpus: launchType === 'EC2' ? undefined : size.vcpus,
            memoryMiB: launchType === 'EC2' ? undefined : size.memoryMiB,
            size: launchType === 'EC2' ? undefined : size.sizeSource,
        });
    } catch (error) {
        log.warn(`[METERING] could not record the Virtual Participant's runtime: ${error}`);
    }
};

// Close the browser context (flushing Chromium's cookie/SQLite state to the
// userDataDir) and upload the profile tar to S3. Guarded so it runs exactly
// once across whichever shutdown path fires first (normal exit, SIGINT/SIGTERM
// from nodemon/Ctrl-C, or an emergency crash).
const closeAndPersistProfile = async (): Promise<void> => {
    if (profilePersisted) return;
    profilePersisted = true;
    try {
        if (browserContext) await browserContext.close();
    } catch (error) {
        log.error('Error closing browser:', error);
    }
    if (activeProfileHandle) {
        try {
            await persistProfile(activeProfileHandle);
        } catch (error) {
            log.error('Error persisting Chromium profile:', error);
        }
        try {
            await releaseProfile(activeProfileHandle);
        } catch (error) {
            log.error('Error releasing Chromium profile:', error);
        }
    }
};

// Local testing mode - skip ALB registration and AppSync updates
const isLocalTest = process.env.LOCAL_TEST === 'true';

// Launch mode. Under MICROVM there is no ALB in front of noVNC: RunMicrovm
// returns a dedicated HTTPS endpoint that the launcher publishes, so the
// container must not attempt to discover its own IP or self-register (there is
// no ECS task metadata endpoint either). See launch-mode.ts.
const needsAlbRegistration = requiresAlbSelfRegistration(process.env.VP_LAUNCH_TYPE);

// Read the build stamp baked into the image at build time (see Dockerfile).
// Logged at startup so it's trivial to confirm which image a task runs.
const readBuildInfo = (): { buildDate: string; gitCommit: string; buildSource: string } => {
    try {
        const raw = readFileSync('/srv/build-info.json', 'utf8');
        const j = JSON.parse(raw);
        return {
            buildDate: j.buildDate || 'unknown',
            gitCommit: j.gitCommit || 'unknown',
            buildSource: j.buildSource || 'unknown',
        };
    } catch {
        return { buildDate: 'unknown', gitCommit: 'unknown', buildSource: 'unknown' };
    }
};

const main = async (): Promise<void> => {
    runStartedMs = Date.now();
    // One container serves one meeting, so its identity is bound once here and
    // every module's log records carry it; the call id is added when known.
    bindLogContext({
        vpId: details.invite.virtualParticipantId,
        meetingPlatform: logMeetingPlatform(details.invite.meetingPlatform),
    });
    const buildInfo = readBuildInfo();
    log.always(
        `LMA Virtual Participant starting... [build ${buildInfo.buildDate} commit ${buildInfo.gitCommit} via ${buildInfo.buildSource}]`,
    );
    if (isLocalTest) {
        log.info('*** LOCAL TEST MODE - Skipping ALB registration and AppSync updates ***');
    }
    log.info(`Meeting Platform: ${details.invite.meetingPlatform}`);
    log.info(`Meeting ID: ${/:\/\//.test(details.invite.meetingId) ? `<link:${details.invite.meetingId.split('://')[1].split(/[/?#]/)[0]}>` : details.invite.meetingId}`);
    log.info(`Meeting Name: ${details.invite.meetingName}`);
    log.info(`LMA User: ${details.lmaUser}`);
    


    // Initialize status manager if VP_ID is provided (skip in local test mode)
    vpId = details.invite.virtualParticipantId || null;
    if (vpId && !isLocalTest) {
        try {
            statusManager = new VirtualParticipantStatusManager(vpId);

            // The meeting password is read from the VP record at startup
            // (startup-meeting-password.ts). Joining without it would fail at
            // the meeting's passcode prompt, or join with no password where
            // one was given, so the launch fails here with the reason instead.
            const startupManager = statusManager;
            await failLaunchIfPasswordUnreadable(startupMeetingPassword, {
                setFailed: (message, endTrigger) => startupManager.setFailed(message, endTrigger),
                exit: (code) => process.exit(code),
                logError: (message) => log.error(message),
            });

            // Get existing CallId from VP record first, with the capture session
            // a rejoined VP runs (docs/virtual-participant.md#rejoin). A row
            // without a CallId is a first launch, which makes its own below. A
            // row that cannot be read is read again, and if it still cannot be,
            // the launch fails: going on would start a rejoined VP's meeting
            // under a new CallId instead of adding to the one it rejoined.
            let existing: VpCallSession | null;
            try {
                const manager = statusManager;
                existing = await readCallSession(() => manager.getCallSession(), {
                    onRetry: (attempt, error) =>
                        log.warn(`Reading the VP record failed (attempt ${attempt}); retrying: ${error}`),
                });
            } catch (readError) {
                log.error(`Could not read the VP record; not starting: ${readError}`);
                try {
                    await statusManager?.setFailed(
                        'Could not read this Virtual Participant\'s record, so it did not start. Try again.',
                        'startup:vp-record-unreadable',
                    );
                } catch (statusError) {
                    log.error(`Failed to record the startup failure: ${statusError}`);
                }
                process.exit(1);
            }
            const existingCallId = existing?.callId;

            if (existing && existingCallId) {
                // Use existing CallId from VP record, for the meeting's events too
                // (kinesis-stream.ts useCallId), so it is the one CallId throughout.
                const { kinesisStreamManager } = await import('./kinesis-stream.js');
                kinesisStreamManager.useCallId(existingCallId);
                kinesisStreamManager.useSession(existing.sessionNumber, existing.sessionTimeOffset);
                process.env.VP_CALL_ID = existingCallId;
                bindLogContext({ callId: existingCallId });
                log.info(
                    existing.sessionNumber > 1
                        ? `Rejoining meeting ${existingCallId} as capture session ${existing.sessionNumber}, ` +
                              `at ${existing.sessionTimeOffset}s on its transcript timeline`
                        : `Using existing VP CallId: ${existingCallId}`,
                );
            } else {
                // Generate new CallId and set it in VP record
                const { kinesisStreamManager } = await import('./kinesis-stream.js');
                const callId = kinesisStreamManager.getCallId();
                await statusManager.setCallId(callId);
                process.env.VP_CALL_ID = callId;
                bindLogContext({ callId });
                log.info(`Generated and set new VP CallId: ${callId}`);
            }
            
            // The agenda and the meeting preparation the VP was started or
            // scheduled with go onto the meeting record in the START event.
            // Read before the meeting starts; a VP without them starts its
            // meeting exactly as before.
            const { kinesisStreamManager: agendaStream } = await import('./kinesis-stream.js');
            const meetingInputs = await statusManager.getMeetingInputs();
            agendaStream.setAgenda(meetingInputs.agenda);
            agendaStream.setPreparation(meetingInputs.preparation);

            // Start with INITIALIZING status
            await statusManager.setInitializing();
            log.always(`VP ${vpId} status: INITIALIZING`);
            
            // Store task ARN for efficient termination
            try {
                await statusManager.storeTaskArnInRegistry();
            } catch (arnError) {
                log.warn(`Failed to store task ARN : ${arnError}`);
            }
        } catch (error) {
            log.error(`Failed to initialize status manager: ${error}`);
        }
    } else if (isLocalTest) {
        log.info('✓ Skipping status manager initialization (local test mode)');
        // Generate a local CallId for testing
        const { kinesisStreamManager } = await import('./kinesis-stream.js');
        const callId = kinesisStreamManager.getCallId();
        process.env.VP_CALL_ID = callId;
        bindLogContext({ callId });
        log.info(`Generated local test CallId: ${callId}`);
    }

    // Wait for VNC server to be ready before proceeding
    log.info('Waiting for VNC server to be ready...');
    let vncReady = false;
    let attempts = 0;
    const maxAttempts = 30; // 30 seconds timeout
    
    while (!vncReady && attempts < maxAttempts) {
        try {
            await fs.access('/tmp/vnc_ready');
            vncReady = true;
            log.info('✓ VNC server is ready');
            break;
        } catch {
            // File doesn't exist yet
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
        attempts++;
    }

    if (!vncReady) {
        log.error('VNC server failed to start within timeout');
        if (statusManager) {
            await statusManager.setFailed('VNC server initialization failed');
        }
        throw new Error('VNC server initialization failed');
    }

    // Register with ALB target group and wait for healthy (skip in local test mode
    // and under MICROVM, which has no ALB — the MicroVM endpoint replaces it).
    if (statusManager && !isLocalTest && needsAlbRegistration) {
        try {
            await statusManager.setRegisteringNetwork();
            log.info('Registering task with ALB target group...');
            const registered = await statusManager.registerWithTargetGroup();
            if (!registered) {
                log.error('Failed to register with target group');
                await statusManager.setFailed('ALB registration failed');
                throw new Error('ALB registration failed');
            }
            log.info('✓ Task registered with ALB and healthy');
        } catch (error) {
            log.error('Error during ALB registration:', error);
            await statusManager.setFailed('ALB registration error');
            throw new Error('ALB registration failed');
        }
    } else if (isLocalTest) {
        log.info('✓ Skipping ALB registration (local test mode)');
    } else if (!needsAlbRegistration) {
        log.info('✓ Skipping ALB registration (MICROVM launch: endpoint published by launcher)');
    }

    // VNC ready signal is deferred until AFTER fresh-profile warmup so the
    // user doesn't see warmup navigation in their live view. Fired below
    // once the browser is ready to navigate to the meeting URL.

    // Calculate sleep time if meeting is scheduled for future
    const currentTimestamp = Math.floor(Date.now() / 1000);
    const timestampDiff = Math.max(0, (details.invite.meetingTime - currentTimestamp - 10) * 1000);
    
    if (timestampDiff > 0) {
        log.info(`Sleeping ${timestampDiff / 1000} seconds until meeting time.`);
        await new Promise(resolve => setTimeout(resolve, timestampDiff));
    }

    // Set CONNECTING status when starting browser
    if (statusManager) {
        await statusManager.setConnecting();
        log.always(`VP ${vpId} status: CONNECTING`);
    }

    // Start the platform-agnostic agent-speaking detector (listens to
    // agent_output.monitor for voice agent PCM output).
    if (voiceAssistant.isEnabled()) {
        try {
            agentSpeakingDetector.start();
        } catch (error) {
            log.error('Failed to start AgentSpeakingDetector (non-critical):', error);
        }
    }

    if (statusManager) {
        await statusManager.setHydratingProfile();
    }
    const profileHandle = await acquireProfile({
        cognitoSub: process.env.LMA_USER_SUB || '',
        platform: details.invite.meetingPlatform,
    });
    activeProfileHandle = profileHandle;

    if (statusManager) {
        await statusManager.setLaunchingBrowser();
    }

    const userDataDir = profileHandle.enabled && profileHandle.localDir
        ? profileHandle.localDir
        : `/srv/cloakbrowser-profiles/ephemeral-${process.pid}`;
    await fs.mkdir(userDataDir, { recursive: true });

    cleanStaleLocks(userDataDir);
    try {
        for (const sub of ['Default/Local Storage/leveldb', 'Default/IndexedDB']) {
            try {
                const items = await fs.readdir(`${userDataDir}/${sub}`, { recursive: true } as any);
                for (const it of items as string[]) {
                    if (typeof it === 'string' && it.endsWith('LOCK')) {
                        try { await fs.unlink(`${userDataDir}/${sub}/${it}`); } catch { /* best-effort */ }
                    }
                }
            } catch { /* may not exist */ }
        }
    } catch (lockErr) {
        log.warn('[profile-store] Stale Chrome lock cleanup failed (continuing):', lockErr);
    }

    // Snapshot freshness before our own writes flip the signal.
    const isFresh = profileIsFresh(userDataDir);

    initProfileDefaults(userDataDir);
    const cookiePatchedCount = patchPreferencesFor3pCookies(userDataDir);
    log.info(`[profile-store] Wrote 3p-cookie allow exceptions for ${cookiePatchedCount} meeting platforms`);
    // Suppress the native "Open <app>?" external-protocol chooser that meeting
    // landing pages (e.g. Webex j.php) raise when they try to launch a desktop
    // client. Headless containers have no such app; the dialog only blocks the
    // web join flow. Must run before launch (Chromium clobbers in-session edits).
    const protoExcludedCount = patchPreferencesForExternalProtocols(userDataDir);
    log.info(`[profile-store] Excluded ${protoExcludedCount} external-protocol schemes (suppresses native app-launch dialog)`);

    // Authenticated joins get a per-user fingerprint (stable across that user's
    // joins). Anonymous / CLI launches have no sub, so fall back to a seed
    // derived from a deployment-stable id (profiles bucket name, else region)
    // rather than a fresh random one per launch — a stable device fingerprint
    // is a weaker bot signal than a never-seen-before one. See
    // stableFingerprintSeed for why this helps Teams anonymous-join CAPTCHA.
    const fingerprintSeed = process.env.LMA_USER_SUB
        ? fingerprintSeedForUser(process.env.LMA_USER_SUB)
        : stableFingerprintSeed(
            process.env.VP_PROFILES_BUCKET || process.env.AWS_REGION || '',
        );

    log.info(`[browser] Launching CloakBrowser persistent context`);
    log.info(`[browser]   userDataDir       = ${userDataDir}`);
    log.info(`[browser]   fingerprint seed  = ${fingerprintSeed}`);
    log.info(`[browser]   profile freshness = ${isFresh ? 'FRESH (warmup will run)' : 'EXISTING (skipping warmup)'}`);

    // Timing instrumentation: browser launch dominated VP startup on MicroVMs
    // (~142s vs ~1-3s for the identical image and args locally), and the launch
    // is a single opaque await. Logging the elapsed time makes any regression
    // visible in CloudWatch instead of requiring a log-timestamp reconstruction.
    const browserLaunchStart = Date.now();
    const context = await launchPersistentContext({
        headless: false,
        humanize: true,
        humanPreset: 'default',
        userDataDir,
        viewport: { width: WINDOW_WIDTH, height: WINDOW_HEIGHT },
        args: (() => {
            const needWebrtc = simliAvatar.isSimliEnabled() || isTeamsSdkJoin();
            log.info(`[browser] WebRTC host-candidate flags: ${needWebrtc ? 'ENABLED' : 'disabled'}`);
            return getCloakLaunchArgs(fingerprintSeed, needWebrtc);
        })(),
        launchOptions: {
            ignoreDefaultArgs: ['--mute-audio', '--enable-automation'],
        },
    } as any);
    log.info(
        `[browser] launchPersistentContext took ${((Date.now() - browserLaunchStart) / 1000).toFixed(1)}s`,
    );
    browserContext = context;
    // Sane default for actions; the one meeting-length wait (end-of-meeting
    // watcher) passes its own explicit { timeout }. Do NOT set a multi-hour
    // default here — it makes every transient wait hang instead of failing fast.
    context.setDefaultTimeout(30_000);

    // Grant camera/microphone to the meeting origins for every join, before
    // warmup or the meeting page loads, so a fresh profile does not get the
    // meeting client's own device-permission dialog. See meeting-permissions.ts.
    const grantedOrigins = await grantMeetingMediaPermissions(context, (origin, err) =>
        log.warn(`[browser] camera/microphone grant failed for ${origin}:`, err),
    );
    log.info(`✓ Camera and microphone permissions granted for ${grantedOrigins.length} meeting origins`);

    await new Promise(resolve => setTimeout(resolve, 2000));
    log.info('✓ Chrome launched with remote debugging on port 9222');

    const platform = details.invite.meetingPlatform;
    const sdkPath = (platform === 'ZOOM' && details.zoomMethod === 'sdk') || isTeamsSdkJoin();
    if (isFresh && !sdkPath) {
        if (statusManager) {
            await statusManager.setWarmingProfile();
        }
        try {
            log.info('[warmup] Profile is fresh — running 3-phase warmup before joining meeting');
            await warmupNavigation(() => context.newPage() as any, {
                runMeetingPlatforms: true,
                log: (m) => log.info(`[browser] ${m}`),
            });
        } catch (err) {
            log.warn('[warmup] Warmup error (non-fatal, continuing to meeting):', err);
        }
    } else if (sdkPath) {
        log.info('[warmup] Skipping warmup (SDK join path does not visit the meeting platform site)');
    }

    // Now that warmup (if any) is done, publish the VNC endpoint so the user's
    // live view opens on the meeting page rather than warmup navigation.
    if (statusManager && !isLocalTest) {
        try {
            await statusManager.setVncReady();
            log.info('✓ VNC endpoint published via AppSync');
        } catch (error) {
            log.error('Failed to publish VNC endpoint:', error);
        }
    } else if (isLocalTest) {
        log.info('✓ Skipping AppSync VNC ready update (local test mode)');
    }

    // NOTE: Simli Avatar is NOT initialized here anymore. Its background
    // page renders the avatar and continuously JPEG-encodes ~20fps frames,
    // which measures at ~1800/2048 CPU units sustained — and on a 2-vCPU
    // host that contends directly with the CPU-heavy Zoom sign-in (Bedrock
    // calls + SPA hydration), stalling sign-in until it escalates to
    // MANUAL_ACTION. Avatar init is deferred into a `prepareAvatar()`
    // callback (defined below) that each platform handler invokes at the
    // right moment: for Zoom, AFTER sign-in completes but BEFORE the
    // prejoin camera is enabled; for guest/other platforms, at the top of
    // the join (unchanged timing, since they have no heavy sign-in phase).

    // Create page BEFORE MCP — external CDP client during newPage() deadlocks target handshake.
    const page = await context.newPage();
    page.setDefaultTimeout(20000);

    // Observational audio diagnostics for GitHub #543. Installed BEFORE any
    // navigation: addInitScript only runs in documents created afterwards, which
    // is why the Simli getUserMedia override (injected once the avatar is ready)
    // never ran on Teams at all. Read-only by design — the previous attempt at
    // this path mutated constraints and silently killed transcription (#545).
    await installAudioDiagnostics(page);
    // A camera that produces black frames, for joins without the avatar: the
    // VP has a microphone but no camera, so a meeting client's combined
    // camera+microphone request fails, which Teams reads as access denied
    // (#1094). Steps aside when the Simli override is in the document, so the
    // avatar path is unchanged. The pre-join flows still turn the camera off.
    // Not on Chime, whose join flow has no camera-off step.
    if (platform !== 'CHIME') {
        // Added after installAudioDiagnostics, so this wrapper is the outer one:
        // a camera request it answers never reaches the [LMA-Audio] failure log,
        // which sees only the microphone part it forwards.
        await page.addInitScript({ content: blankCameraInitScript() });
        log.info('✓ Blank camera installed for camera requests without the avatar');
    }
    // Node-side draining: page console.log does NOT reach CloudWatch under
    // cloakbrowser (only warnings/errors do), which is why the first version of
    // these diagnostics produced nothing across three live sessions.
    const stopAudioDiagnostics = startAudioDiagnosticsPolling(page);
    // Keep the meeting client's Web Audio graph off the transmit path (Teams
    // chops the agent's voice inside it — see startSenderTrackBypass).
    const stopSenderBypass = startSenderTrackBypass(page);
    // PulseAudio device formats, logged only when they change. Teams opens the
    // virtual mic twice with conflicting formats (mono vs stereo); with
    // avoid-resampling that can make PulseAudio re-negotiate the device
    // repeatedly, which glitches audio without dropping any RTP packet (#543).
    const stopDeviceSpecs = startAudioDeviceSpecPolling();
    stopPageMonitors = () => {
        stopAudioDiagnostics();
        stopSenderBypass();
        stopDeviceSpecs();
    };

    // Renderer-crash latch. A renderer OOM crash leaves every page.evaluate
    // hanging on "Target crashed", so meeting.initialize() never returns and
    // the UI stays stuck on JOINING. We race initialize() against this latch
    // (below) so a crash deterministically loses the race and drives the
    // catch → FAILED path instead of an indefinite hang. Settled at most once.
    let rejectOnPageCrash: (err: Error) => void = () => {};
    let pageCrashSettled = false;
    const pageCrashLatch = new Promise<never>((_, reject) => {
        rejectOnPageCrash = (err: Error) => {
            if (pageCrashSettled) return;
            pageCrashSettled = true;
            reject(err);
        };
    });
    // Never let an unconsumed rejection crash the process; the race below is
    // the real consumer.
    pageCrashLatch.catch(() => {});

    // Forward meeting-page console output (getUserMedia override, Simli bridge)
    // to container logs. NOTE: we deliberately do NOT install a global
    // console.* wrapper via addInitScript here. An earlier attempt to do so (an
    // exposeFunction '__lmaLog' bridge + per-frame console patching) interfered
    // with the meeting page under cloakbrowser and broke the speaker
    // MutationObserver's exposeFunction callbacks (speakerChange never fired →
    // every turn labelled "none"). The simpler page.on('console') below matches
    // the working baseline; the platform handler attaches its own richer
    // console listener once it starts.
    page.on('console', (msg) => {
        const text = msg.text();
        const type = msg.type();
        if (
            text.includes('[LMA-Simli]') ||
            text.includes('[Simli]') ||
            // Audio path diagnostics (#543): requested constraints, the settings
            // actually applied, and outbound Opus packet cadence.
            text.includes('[LMA-Audio]') ||
            type === 'error' ||
            type === 'warning'
        ) {
            log.info(`Browser ${type}: ${text}`);
        }
    });
    page.on('pageerror', (err) => log.warn('MeetingPage error:', err?.message || err));
    page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) {
            log.info(`[meeting-page] navigated → ${frame.url()}`);
        } else {
            log.info(`[meeting-page] subframe navigated → ${frame.url()}`);
        }
    });
    // Page-lifecycle diagnostics.
    page.on('close', () => {
        log.warn('[page-lifecycle] page CLOSED event fired');
    });
    page.on('crash', () => {
        // A renderer crash (most commonly an OOM kill — "Aw, Snap! Error
        // code 9" — when memory hits the task cap) leaves every subsequent
        // page.evaluate hanging on "Target crashed". Without this, the join
        // sequence stalls forever and the UI stays stuck on JOINING. Reject
        // the crash latch so the meeting init loses the race below and the
        // catch path drives a clean FAILED + task exit.
        log.error('[page-lifecycle] page CRASHED (renderer crash) — aborting join');
        rejectOnPageCrash(new Error('Renderer crashed (likely out of memory) — the meeting page died mid-join'));
    });
    page.on('framedetached', (frame) => {
        const isMain = frame === page.mainFrame();
        log.info(`[meeting-page] frame detached (mainFrame=${isMain}) url=${frame.url()}`);
    });
    context.on('close', () => {
        log.warn('[browser-lifecycle] browser context CLOSED');
    });

    // Initialize MCP command handler AFTER browser is launched and the
    // meeting page exists.
    if (statusManager && vpId) {
        try {
            const callId = process.env.VP_CALL_ID || '';
            if (callId) {
                mcpHandler = new MCPCommandHandler(vpId, callId);
                await mcpHandler.start();
                log.info('✓ MCP command handler started');
            } else {
                log.info('VP_CALL_ID not set - skipping MCP handler');
            }
        } catch (error) {
            log.error('Failed to start MCP command handler:', error);
            // Non-critical - continue with meeting join
        }
    }

    // Start Strands Lambda warmup timer to keep MCP connections alive during meeting
    // Sends a lightweight {action: 'warmup'} ping every 3 minutes
    const strandsLambdaArn = process.env.STRANDS_LAMBDA_ARN;
    if (strandsLambdaArn) {
        try {
            const lambdaClient = new LambdaClient({
                region: process.env.AWS_REGION || 'us-east-1',
            });
            const WARMUP_INTERVAL = 3 * 60 * 1000; // 3 minutes

            const sendWarmupPing = async () => {
                try {
                    const command = new InvokeCommand({
                        FunctionName: strandsLambdaArn,
                        InvocationType: 'RequestResponse',
                        Payload: JSON.stringify({ action: 'warmup' }),
                    });
                    const response = await lambdaClient.send(command);
                    const payload = JSON.parse(new TextDecoder().decode(response.Payload));
                    log.info(`🔥 Strands warmup ping: ${payload.mcp_clients} MCP clients, from_cache=${payload.from_cache}, ${payload.warmup_time_ms}ms`);
                } catch (err) {
                    log.warn('Strands warmup ping failed (non-critical):', err);
                }
            };

            // Send initial warmup immediately to pre-warm before first user message
            sendWarmupPing();

            // Then send every 3 minutes to keep Lambda container and MCP connections alive
            strandsWarmupTimer = setInterval(sendWarmupPing, WARMUP_INTERVAL);
            log.info(`✓ Strands Lambda warmup timer started (every ${WARMUP_INTERVAL / 1000}s)`);
        } catch (error) {
            log.warn('Failed to start Strands warmup timer (non-critical):', error);
        }
    }

    // Simli Avatar deferred setup. Each platform handler calls this via the
    // `prepareAvatar` option at the right moment (Zoom: after sign-in, before
    // the prejoin camera is enabled; others: at the top of the join). This
    // both (a) brings the avatar up only when it's about to be needed as the
    // camera, and (b) keeps its sustained ~1800-CPU-unit render+encode load
    // off the CPU-heavy sign-in phase. Idempotent + concurrency-safe via the
    // run-once guard, so multiple calls (e.g. framenavigated + handler) are
    // cheap no-ops after the first.
    let avatarPrepared: Promise<void> | null = null;
    const prepareAvatar = async (): Promise<void> => {
        if (!simliAvatar.isSimliEnabled()) return;
        if (avatarPrepared) return avatarPrepared;
        avatarPrepared = (async () => {
            try {
                log.info('Initializing Simli Avatar...');
                await simliAvatar.initialize(context);
                log.info('✓ Simli Avatar initialized');
            } catch (error) {
                log.error('Failed to initialize Simli Avatar (non-critical):', error);
                return; // No avatar — meeting proceeds without it.
            }
            // Initializing the Simli avatar opened a second tab (local.simli),
            // which becomes the foreground tab and hides the meeting in the VNC
            // viewer. Bring the meeting page back to the front so the human
            // watching VNC sees the meeting by default.
            try {
                await page.bringToFront();
            } catch (e) {
                /* non-critical */
            }
            if (!simliAvatar.isConnected()) return;
            try {
                // Camera/microphone permissions for the meeting origins were
                // already granted right after the browser launched (see
                // grantMeetingMediaPermissions), for every join.
                //
                // Inject the getUserMedia override AND start the Node-side
                // WebRTC bridge poll loop (both live inside
                // injectGetUserMediaOverride). The poll loop scans page.frames()
                // and services any frame that calls getUserMedia({video}) —
                // platform-agnostic, picks up new subframes/navigations
                // automatically, and self-heals if the bridge drops mid-call. So
                // no framenavigated wiring or on-demand-reconnect plumbing is
                // needed here. The avatar is downscaled to 256x256@15fps before
                // the bridge so Zoom's video encoder stays cheap enough for a
                // 2-vCPU host (t3.medium).
                await simliAvatar.injectGetUserMediaOverride(page);
                log.info('✓ Simli getUserMedia override + WebRTC bridge installed');
            } catch (error) {
                log.error('Failed to set up Simli avatar for meeting (non-critical):', error);
            }
        })();
        return avatarPrepared;
    };

    let meeting: Chime | Zoom | ZoomSdk | Teams | TeamsSdk | Webex | GoogleMeet;
    let success = false;
    let exitInfo: ExitInfo | null = null;

    try {
        // Set JOINING status before attempting to join meeting
        if (statusManager) {
            await statusManager.setJoining();
            log.always(`VP ${vpId} status: JOINING`);
        }

        // Initialize the appropriate meeting platform handler
        log.info(`Initializing ${details.invite.meetingPlatform} handler...`);
        log.debug(`Meeting platform value: "${details.invite.meetingPlatform}" (type: ${typeof details.invite.meetingPlatform})`);

        switch (details.invite.meetingPlatform) {
            case 'CHIME':
                meeting = new Chime();
                break;
            case 'ZOOM':
                meeting = details.zoomMethod === 'sdk' ? new ZoomSdk() : new Zoom();
                log.info(`Zoom join method: ${details.zoomMethod}`);
                break;
            case 'TEAMS':
            case 'Teams':
                meeting = details.teamsMethod === 'sdk' ? new TeamsSdk() : new Teams();
                log.info(`Teams join method: ${details.teamsMethod}`);
                break;
            case 'WEBEX':
                meeting = new Webex();
                break;
            case 'GOOGLE_MEET':
                meeting = new GoogleMeet();
                break;
            default:
                throw new Error(`Unsupported meeting platform: ${details.invite.meetingPlatform}`);
        }
        // Reads `meeting` when called, so it follows the hand-over from an ACS
        // join to the browser join below.
        leaveActiveMeeting = () => meeting.leaveMeeting(page);

        // The meeting's consent mode (docs/meeting-consent.md). An opt-in
        // meeting starts with capture withheld (details.start off) and its
        // screen recording waits for consent; the platform handler starts the
        // rest of the flow once it is in the meeting.
        const consentPlan = await prepareMeetingConsent();

        // Start recording service
        recordingService.startRecording();

        // Start screen (video) recording in parallel with audio. No-op unless
        // ENABLE_VIDEO_RECORDING is set; runs independently of audio recording.
        // Started by the consent flow instead when it waits for consent.
        if (!consentPlan.deferVideo) {
            try {
                await videoRecorder.start(process.env.VP_CALL_ID || '');
            } catch (error) {
                log.error('Error starting video recording:', error);
            }
        }

        // Key frames of the shared screen, stored on the meeting. Same switch
        // as the screen recording; samples are only judged once the meeting
        // is being transcribed (see keyframe-sampler.ts).
        if (details.enableVideoRecording && details.recordingsBucketName) {
            try {
                const { kinesisStreamManager } = await import('./kinesis-stream.js');
                // Whether the platform has a screen-share signal is known now;
                // which probe reads it is decided at each sample, because the
                // Teams one depends on the join method, and a refused ACS join
                // hands over to the browser join after this has started.
                const sharePlatform = details.invite.meetingPlatform;
                const hasShareProbe = screenShareProbeFor(sharePlatform, details.teamsMethod) !== null;
                keyframeSampler.start({
                    callId: kinesisStreamManager.currentCallId,
                    bucket: details.recordingsBucketName,
                    isLive: () => transcriptionService.transcribingLive,
                    // On the meeting's transcript timeline, like the segments: a
                    // rejoined VP's session starts at its offset, not at 0.
                    timelineSeconds: () =>
                        kinesisStreamManager.sessionOffsetSeconds + transcriptionService.timelineSeconds(),
                    recordingStartedAtMs: () => videoRecorder.startedAtMs,
                    shareActive: hasShareProbe
                        ? () => {
                              const probe = screenShareProbeFor(sharePlatform, details.teamsMethod);
                              return probe ? probe(page) : Promise.resolve(null);
                          }
                        : undefined,
                    emit: (frame) => kinesisStreamManager.sendVideoKeyframe(frame),
                });
            } catch (error) {
                log.error('Error starting key frame sampling:', error);
            }
        }

        // Join the meeting and wait for it to end. The platform handler
        // returns a structured ExitInfo describing WHY the meeting ended;
        // we log it canonically and persist a human-readable form alongside
        // the COMPLETED status for the UI.
        // Race against the renderer-crash latch so an OOM crash mid-join
        // rejects here (→ catch → FAILED) instead of hanging forever on a
        // dead "Target crashed" page.evaluate.
        //
        // A Teams join through Azure Communication Services hands over to the
        // browser join, in the same meeting and on the same page, when Teams
        // refuses it for a reason the meeting's tenant or meeting type decides
        // (teams-join-method.ts) and the join method was chosen automatically.
        // TeamsSdk.initialize has by then left the call, closed its embed
        // server, deleted the ACS identity and taken its listeners off the
        // page, in its own finally. It refuses before admission, so nothing
        // it starts once in the meeting (media, consent, chat and speaker
        // monitors) is running; what index.ts started above is the same for
        // both joins, and prepareAvatar runs once.
        const joinMeeting = async (): Promise<ExitInfo> => {
            try {
                return await meeting.initialize(page, { prepareAvatar });
            } catch (error) {
                if (
                    !(meeting instanceof TeamsSdk) ||
                    !(error instanceof AcsJoinRefusedError) ||
                    !teamsSdkFallbackAllowed(process.env.MEETING_TEAMS_METHOD)
                ) {
                    throw error;
                }
                log.always(acsFallbackLogLine(error.subCode));
                noteAcsRefusal(error.subCode);
                if (statusManager) {
                    await statusManager.setJoiningSubstep(acsFallbackSubstep(error.subCode));
                }
                // Read again by the screen-share probe above, at each sample.
                details.teamsMethod = 'dom';
                // Off the embed server's page, which is closed by now, before
                // the Teams web client loads.
                await page.goto('about:blank').catch(() => {});
                meeting = new Teams();
                log.info(`Teams join method: ${details.teamsMethod} (after Teams refused the ACS join)`);
                return meeting.initialize(page, { prepareAvatar });
            }
        };
        exitInfo = await Promise.race([joinMeeting(), pageCrashLatch]);
        // The meeting is over for the VP: the dialog watchdog stands down.
        noteMeetingOver();

        const exitDetailParts = [
            `reason=${exitInfo.reason}`,
            `trigger=${exitInfo.trigger ?? 'n/a'}`,
            exitInfo.requestedBy ? `requestedBy=${JSON.stringify(exitInfo.requestedBy)}` : null,
            exitInfo.matchedMessage ? `message=${JSON.stringify(exitInfo.matchedMessage)}` : null,
        ].filter(Boolean);
        log.always(`VP exit: ${exitDetailParts.join(' ')}`);
        log.info('Meeting session completed successfully');
        success = true;


    } catch (error: any) {
        log.error('Meeting failed:', error.message);

        // After a stop signal the join fails because the signal path closed
        // the browser; that path records how the participant ended.
        if (statusManager && !shutdownRequested) {
            // A thrown error carries no ExitInfo, so the prose shown to the
            // user is still derived by matching on the message. Each branch
            // also records which branch it took, so these rows are countable
            // alongside the ones that arrive with a real trigger.
            const errorMsg = error.message.toLowerCase();
            if (errorMsg.includes('renderer crashed') || errorMsg.includes('target crashed')) {
                await statusManager.setFailed('The meeting browser ran out of memory and crashed during join. Please try again.', 'error:renderer-crashed');
            } else if (errorMsg.includes('password') || errorMsg.includes('passcode')) {
                await statusManager.setFailed('Wrong meeting password', 'error:password');
            } else if (errorMsg.includes('meeting not found') || errorMsg.includes('invalid meeting')) {
                await statusManager.setFailed('Invalid meeting ID', 'error:meeting-not-found');
            } else if (errorMsg.includes('meeting ended') || errorMsg.includes('meeting has ended')) {
                await statusManager.setFailed('Meeting already ended', 'error:meeting-ended');
            } else if (errorMsg.includes('permission denied') || errorMsg.includes('not authorized')) {
                await statusManager.setFailed('Permission denied', 'error:permission-denied');
            } else {
                await statusManager.setFailed(`Meeting join failed: ${error.message}`, 'error:unclassified');
            }
        }
        
    } finally {
        // A stop signal has its own shutdown (signalHandler below), which runs
        // the same end-of-meeting work (meetingTeardown), records the status
        // and exits. The handler's wait for the meeting to end can return
        // during it (the leave makes the meeting end on the page), and running
        // this cleanup as well would overwrite the recorded status.
        if (shutdownRequested) {
            log.info('Stop requested: the signal handler is shutting down.');
            return;
        }
        // Cleanup - set flag to prevent uncaughtException from killing process mid-cleanup
        cleanupInProgress = true;
        log.info('Cleaning up...');

        // The end-of-meeting work shared with the stop-signal path
        // (meeting-teardown.ts): the consent flow, the leave when the VP is
        // still in the meeting, key frames, transcription, the recordings and
        // their links, and END.
        await meetingTeardown.run({
            stopping: false,
            leaveReason: exitInfo && shouldLeaveAfterExit(exitInfo) ? `ended: ${exitInfo.reason}` : null,
        });

        // On the FAILED path, wait for the user to click "Got it" on the
        // failure banner before tearing down the ALB/VNC mapping. Otherwise
        // the VNC viewer goes black the instant we set FAILED and the user
        // never gets to see why (or to use the browser to inspect the page
        // that tripped us up). Capped at 10 min so a user who's walked away
        // doesn't pay for an idle Fargate task indefinitely.
        if (!success && statusManager) {
            const HARD_CAP_MS = 600_000;
            const POLL_MS = 3_000;
            const deadline = Date.now() + HARD_CAP_MS;
            log.info(`Waiting up to ${HARD_CAP_MS / 1000}s for user to acknowledge failure before tearing down VNC...`);
            while (Date.now() < deadline) {
                if (await statusManager.getUserAcknowledgedFailure()) {
                    log.info('✓ User acknowledged failure — proceeding with tear-down');
                    break;
                }
                await new Promise((r) => setTimeout(r, POLL_MS));
            }
            if (Date.now() >= deadline) {
                log.info('Failure-ack hard cap reached — proceeding with tear-down');
            }
        }

        // Deregister from ALB target group
        if (statusManager) {
            try {
                await statusManager.deregisterFromTargetGroup();
                log.info('✓ Deregistered from ALB target group');
            } catch (error) {
                log.error('Error deregistering from ALB:', error);
            }
        }

        await releaseServices();

        // Close the browser context (persistent context owns the browser) and
        // sync the profile back to S3 so the next launch resumes the session.
        await closeAndPersistProfile();

        // Final status update. A returned exitInfo doesn't always mean
        // success: the handler returns 'never-joined' when the VP never
        // actually entered the meeting (prejoin timeout / not admitted /
        // stuck on the join form). That must be surfaced as FAILED — not
        // COMPLETED — so the UI doesn't falsely report success on a meeting
        // the VP never joined. Genuine completions persist the human-readable
        // exit detail (e.g. "Asked to leave by Jeremy") alongside COMPLETED.
        // The structured trigger is persisted next to the prose, so "why did
        // joins fail today" is a query over the VP table rather than a search
        // through container logs. Normalised on the way in so it stays a value
        // rows can be grouped by: a handful of triggers interpolate a platform
        // error message, and the full prose has its home in errorMessage. The
        // log line above keeps the trigger exactly as the handler reported it.
        const endTrigger = normalizeExitTrigger(exitInfo?.trigger);
        const joined = exitInfo ? didJoinMeeting(exitInfo) : true;
        await meterRuntime();
        if (success && joined) {
            const completionMessage = exitInfo ? formatExitMessage(exitInfo) : undefined;
            if (statusManager) {
                await statusManager.setCompleted(completionMessage, endTrigger);
            }
            log.always(
                completionMessage
                    ? `LMA Virtual Participant completed: ${completionMessage}`
                    : 'LMA Virtual Participant completed successfully',
            );
            process.exit(0);
        } else if (success && !joined) {
            // Ran cleanly but never joined → FAILED with the reason.
            const failMessage = exitInfo ? formatExitMessage(exitInfo) : 'Could not join the meeting.';
            if (statusManager) {
                await statusManager.setFailed(failMessage, endTrigger);
            }
            log.always(`LMA Virtual Participant did not join: ${failMessage}`);
            process.exit(1);
        } else {
            log.always('LMA Virtual Participant failed');
            process.exit(1);
        }
        
        log.info('Ending Task. Bye.');
        process.exit(1);
    }
};

// Handle process signals for graceful shutdown 
const signalHandler = async (signal: string) => {
    if (shutdownRequested) {
        log.always(`Received ${signal} while already shutting down; ignored`);
        return;
    }
    log.always(`Received ${signal}, initiating graceful shutdown...`);
    shutdownRequested = true;
    // The teardown below runs the whole end of the meeting; a stray rejection in
    // it is logged rather than turned into an emergency FAILED exit, as during
    // main()'s own cleanup.
    cleanupInProgress = true;
    // From here on the setup or join code still running reports no further
    // progress; only the terminal status below is written.
    beginShutdown();

    // The same end-of-meeting work as main()'s cleanup (meeting-teardown.ts),
    // ordered for the stop grace period: leave the meeting on the platform
    // first, while the browser is still open (bounded by LEAVE_BUDGET_MS;
    // without it the meeting keeps listing the VP until the platform times
    // out its connection, #1127), then key frames, transcription, the
    // recordings and their links, and END. If main()'s cleanup has already
    // started it, this waits for that run instead of repeating it.
    await meetingTeardown.run({ stopping: true, leaveReason: `stopped by ${signal}` });

    // Update status to COMPLETED when externally terminated
    if (statusManager && vpId) {
        try {
            await statusManager.setCompleted(); // Use COMPLETED for external termination
            log.always(`VP ${vpId} status updated to COMPLETED due to external termination`);
        } catch (error) {
            log.error(`Failed to update status during shutdown: ${error}`);
        }
    }

    // Deregister from ALB target group
    if (statusManager) {
        try {
            await statusManager.deregisterFromTargetGroup();
            log.info('✓ Deregistered from ALB target group');
        } catch (error) {
            log.error('Error deregistering from ALB:', error);
        }
    }

    await releaseServices();

    // Close the browser and flush the profile to S3 so the session is
    // remembered next launch. nodemon/Ctrl-C deliver SIGTERM/SIGINT here, so
    // without this the latest session would never be persisted.
    await closeAndPersistProfile();
    await meterRuntime();

    log.info('Graceful shutdown complete. Exiting...');
    process.exit(0);
};

process.on('SIGINT', () => signalHandler('SIGINT'));
process.on('SIGTERM', () => signalHandler('SIGTERM'));

/**
 * Emergency cleanup for uncaught exceptions and unhandled rejections.
 * Ensures the meeting doesn't stay stuck as "in progress" in LMA when the
 * ECS task crashes unexpectedly (e.g., ERR_STREAM_PREMATURE_CLOSE from
 * transcription pipeline failures, expired Transcribe sessions, etc.).
 * 
 * This sends the END event to Kinesis (so the call_event_processor marks
 * the meeting as ended) and updates the VP status to FAILED in DynamoDB.
 */
const emergencyCleanup = async (errorMessage: string): Promise<void> => {
    // Set flag to prevent recursive cleanup if another exception fires during this
    cleanupInProgress = true;
    beginShutdown();
    log.info('Performing emergency cleanup before exit...');

    // Send END event to Kinesis so the meeting is marked as ended
    try {
        log.info('Emergency: Sending END meeting event to Kinesis...');
        await sendEndMeeting();
        log.info('Emergency: END meeting event sent successfully');
    } catch (endError) {
        log.error('Emergency: Failed to send END meeting event:', endError);
    }

    // Update VP status to FAILED so it doesn't show as in-progress
    if (statusManager) {
        try {
            log.warn('Emergency: Updating VP status to FAILED...');
            await statusManager.setFailed(errorMessage);
            log.warn('Emergency: VP status updated to FAILED');
        } catch (statusError) {
            log.error('Emergency: Failed to update VP status:', statusError);
        }

        // Deregister from ALB target group
        try {
            await statusManager.deregisterFromTargetGroup();
            log.info('Emergency: Deregistered from ALB target group');
        } catch (albError) {
            log.error('Emergency: Failed to deregister from ALB:', albError);
        }
    }

    // Stop Strands warmup timer
    if (strandsWarmupTimer) {
        clearInterval(strandsWarmupTimer);
        strandsWarmupTimer = null;
    }

    // Best-effort: flush the profile to S3 even on a crash so a partially
    // established session isn't lost.
    await closeAndPersistProfile();

    log.info('Emergency cleanup complete');
};

// Handle uncaught exceptions - always attempt cleanup before exiting
process.on('uncaughtException', async (error: any) => {
    if (cleanupInProgress) {
        // During cleanup, log but don't exit - let the finally block complete
        log.error('Uncaught Exception during cleanup (non-fatal):', error.message || error);
    } else {
        log.error('Uncaught Exception:', error);
        // Perform emergency cleanup before exiting to ensure meeting status is updated
        // and the meeting doesn't stay stuck as "in progress" in LMA
        await emergencyCleanup('Uncaught exception: ' + (error.message || error));
        process.exit(1);
    }
});

process.on('unhandledRejection', async (reason: any, promise: any) => {
    if (cleanupInProgress) {
        // During cleanup, log but don't exit - let the finally block complete
        log.error('Unhandled Rejection during cleanup (non-fatal):', reason);
    } else {
        log.error('Unhandled Rejection at:', promise, 'reason:', reason);
        // Perform emergency cleanup before exiting to ensure meeting status is updated
        await emergencyCleanup('Unhandled rejection: ' + (reason?.message || reason));
        process.exit(1);
    }
});

// Start the application
main().catch(async (error) => {
    log.error('Application failed to start:', error);
    await emergencyCleanup('Application startup failed: ' + (error.message || error));
    process.exit(1);
});
