import { launchPersistentContext } from 'cloakbrowser';
import { promises as fs, readFileSync } from 'fs';
import Chime from './chime.js';
import Zoom from './zoom.js';
import ZoomSdk from './zoom-sdk.js';
import Teams from './teams.js';
import TeamsSdk from './teams-sdk.js';
import Webex from './webex.js';
import { details, ExitInfo, formatExitMessage, didJoinMeeting } from './details.js';
import { transcriptionService } from './scribe.js';
import { VirtualParticipantStatusManager } from './status-manager.js';
import { recordingService } from './recording.js';
import { videoRecorder } from './video-recorder.js';
import { sendEndMeeting, sendStartMeeting } from './kinesis-stream.js';
import { MCPCommandHandler } from './mcp-command-handler.js';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { simliAvatar } from './simli-avatar.js';
import { voiceAssistant } from './voice-assistant.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { acquireProfile, persistProfile, releaseProfile } from './profile-store.js';
import { requiresAlbSelfRegistration } from './launch-mode.js';
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

// Match the Xvfb screen size; fluxbox toolbar is suppressed in entrypoint.sh.
const WINDOW_WIDTH = 1920;
const WINDOW_HEIGHT = 1080 - 130;

const isTeamsPlatform = (platform: string): boolean => platform.toUpperCase() === 'TEAMS';

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
        console.error('Error closing browser:', error);
    }
    if (activeProfileHandle) {
        try {
            await persistProfile(activeProfileHandle);
        } catch (error) {
            console.error('Error persisting Chromium profile:', error);
        }
        try {
            await releaseProfile(activeProfileHandle);
        } catch (error) {
            console.error('Error releasing Chromium profile:', error);
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
    const buildInfo = readBuildInfo();
    console.log(
        `LMA Virtual Participant starting... [build ${buildInfo.buildDate} commit ${buildInfo.gitCommit} via ${buildInfo.buildSource}]`,
    );
    if (isLocalTest) {
        console.log('*** LOCAL TEST MODE - Skipping ALB registration and AppSync updates ***');
    }
    console.log(`Meeting Platform: ${details.invite.meetingPlatform}`);
    console.log(`Meeting ID: ${details.invite.meetingId}`);
    console.log(`Meeting Name: ${details.invite.meetingName}`);
    console.log(`LMA User: ${details.lmaUser}`);
    


    // Initialize status manager if VP_ID is provided (skip in local test mode)
    vpId = details.invite.virtualParticipantId || null;
    if (vpId && !isLocalTest) {
        try {
            statusManager = new VirtualParticipantStatusManager(vpId);
            
            // Get existing CallId from VP record first
            const existingCallId = await statusManager.getCallId();
            
            if (existingCallId) {
                // Use existing CallId from VP record
                process.env.VP_CALL_ID = existingCallId;
                console.log(`Using existing VP CallId: ${existingCallId}`);
            } else {
                // Generate new CallId and set it in VP record
                const { kinesisStreamManager } = await import('./kinesis-stream.js');
                const callId = kinesisStreamManager.getCallId();
                await statusManager.setCallId(callId);
                process.env.VP_CALL_ID = callId;
                console.log(`Generated and set new VP CallId: ${callId}`);
            }
            
            // Start with INITIALIZING status
            await statusManager.setInitializing();
            console.log(`VP ${vpId} status: INITIALIZING`);
            
            // Store task ARN for efficient termination
            try {
                await statusManager.storeTaskArnInRegistry();
            } catch (arnError) {
                console.log(`Failed to store task ARN : ${arnError}`);
            }
        } catch (error) {
            console.error(`Failed to initialize status manager: ${error}`);
        }
    } else if (isLocalTest) {
        console.log('✓ Skipping status manager initialization (local test mode)');
        // Generate a local CallId for testing
        const { kinesisStreamManager } = await import('./kinesis-stream.js');
        const callId = kinesisStreamManager.getCallId();
        process.env.VP_CALL_ID = callId;
        console.log(`Generated local test CallId: ${callId}`);
    }

    // Wait for VNC server to be ready before proceeding
    console.log('Waiting for VNC server to be ready...');
    let vncReady = false;
    let attempts = 0;
    const maxAttempts = 30; // 30 seconds timeout
    
    while (!vncReady && attempts < maxAttempts) {
        try {
            await fs.access('/tmp/vnc_ready');
            vncReady = true;
            console.log('✓ VNC server is ready');
            break;
        } catch {
            // File doesn't exist yet
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
        attempts++;
    }

    if (!vncReady) {
        console.error('VNC server failed to start within timeout');
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
            console.log('Registering task with ALB target group...');
            const registered = await statusManager.registerWithTargetGroup();
            if (!registered) {
                console.error('Failed to register with target group');
                await statusManager.setFailed('ALB registration failed');
                throw new Error('ALB registration failed');
            }
            console.log('✓ Task registered with ALB and healthy');
        } catch (error) {
            console.error('Error during ALB registration:', error);
            await statusManager.setFailed('ALB registration error');
            throw new Error('ALB registration failed');
        }
    } else if (isLocalTest) {
        console.log('✓ Skipping ALB registration (local test mode)');
    } else if (!needsAlbRegistration) {
        console.log('✓ Skipping ALB registration (MICROVM launch: endpoint published by launcher)');
    }

    // VNC ready signal is deferred until AFTER fresh-profile warmup so the
    // user doesn't see warmup navigation in their live view. Fired below
    // once the browser is ready to navigate to the meeting URL.

    // Calculate sleep time if meeting is scheduled for future
    const currentTimestamp = Math.floor(Date.now() / 1000);
    const timestampDiff = Math.max(0, (details.invite.meetingTime - currentTimestamp - 10) * 1000);
    
    if (timestampDiff > 0) {
        console.log(`Sleeping ${timestampDiff / 1000} seconds until meeting time.`);
        await new Promise(resolve => setTimeout(resolve, timestampDiff));
    }

    // Set CONNECTING status when starting browser
    if (statusManager) {
        await statusManager.setConnecting();
        console.log(`VP ${vpId} status: CONNECTING`);
    }

    // Start the platform-agnostic agent-speaking detector (listens to
    // agent_output.monitor for voice agent PCM output).
    if (voiceAssistant.isEnabled()) {
        try {
            agentSpeakingDetector.start();
        } catch (error) {
            console.error('Failed to start AgentSpeakingDetector (non-critical):', error);
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
        console.warn('[profile-store] Stale Chrome lock cleanup failed (continuing):', lockErr);
    }

    // Snapshot freshness before our own writes flip the signal.
    const isFresh = profileIsFresh(userDataDir);

    initProfileDefaults(userDataDir);
    const cookiePatchedCount = patchPreferencesFor3pCookies(userDataDir);
    console.log(`[profile-store] Wrote 3p-cookie allow exceptions for ${cookiePatchedCount} meeting platforms`);
    // Suppress the native "Open <app>?" external-protocol chooser that meeting
    // landing pages (e.g. Webex j.php) raise when they try to launch a desktop
    // client. Headless containers have no such app; the dialog only blocks the
    // web join flow. Must run before launch (Chromium clobbers in-session edits).
    const protoExcludedCount = patchPreferencesForExternalProtocols(userDataDir);
    console.log(`[profile-store] Excluded ${protoExcludedCount} external-protocol schemes (suppresses native app-launch dialog)`);

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

    console.log(`[browser] Launching CloakBrowser persistent context`);
    console.log(`[browser]   userDataDir       = ${userDataDir}`);
    console.log(`[browser]   fingerprint seed  = ${fingerprintSeed}`);
    console.log(`[browser]   profile freshness = ${isFresh ? 'FRESH (warmup will run)' : 'EXISTING (skipping warmup)'}`);

    const context = await launchPersistentContext({
        headless: false,
        humanize: true,
        humanPreset: 'default',
        userDataDir,
        viewport: { width: WINDOW_WIDTH, height: WINDOW_HEIGHT },
        args: (() => {
            const needWebrtc = simliAvatar.isSimliEnabled() || isTeamsSdkJoin();
            console.log(`[browser] WebRTC host-candidate flags: ${needWebrtc ? 'ENABLED' : 'disabled'}`);
            return getCloakLaunchArgs(fingerprintSeed, needWebrtc);
        })(),
        launchOptions: {
            ignoreDefaultArgs: ['--mute-audio', '--enable-automation'],
        },
    } as any);
    browserContext = context;
    // Sane default for actions; the one meeting-length wait (end-of-meeting
    // watcher) passes its own explicit { timeout }. Do NOT set a multi-hour
    // default here — it makes every transient wait hang instead of failing fast.
    context.setDefaultTimeout(30_000);

    await new Promise(resolve => setTimeout(resolve, 2000));
    console.log('✓ Chrome launched with remote debugging on port 9222');

    const platform = details.invite.meetingPlatform;
    const sdkPath = (platform === 'ZOOM' && details.zoomMethod === 'sdk') || isTeamsSdkJoin();
    if (isFresh && !sdkPath) {
        if (statusManager) {
            await statusManager.setWarmingProfile();
        }
        try {
            console.log('[warmup] Profile is fresh — running 3-phase warmup before joining meeting');
            await warmupNavigation(() => context.newPage() as any, {
                runMeetingPlatforms: true,
                log: (m) => console.log(`[browser] ${m}`),
            });
        } catch (err) {
            console.warn('[warmup] Warmup error (non-fatal, continuing to meeting):', err);
        }
    } else if (sdkPath) {
        console.log('[warmup] Skipping warmup (SDK join path does not visit the meeting platform site)');
    }

    // Now that warmup (if any) is done, publish the VNC endpoint so the user's
    // live view opens on the meeting page rather than warmup navigation.
    if (statusManager && !isLocalTest) {
        try {
            await statusManager.setVncReady();
            console.log('✓ VNC endpoint published via AppSync');
        } catch (error) {
            console.error('Failed to publish VNC endpoint:', error);
        }
    } else if (isLocalTest) {
        console.log('✓ Skipping AppSync VNC ready update (local test mode)');
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
            type === 'error' ||
            type === 'warning'
        ) {
            console.log(`Browser ${type}: ${text}`);
        }
    });
    page.on('pageerror', (err) => console.warn('MeetingPage error:', err?.message || err));
    page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) {
            console.log(`[meeting-page] navigated → ${frame.url()}`);
        } else {
            console.log(`[meeting-page] subframe navigated → ${frame.url()}`);
        }
    });
    // Page-lifecycle diagnostics.
    page.on('close', () => {
        console.warn('[page-lifecycle] page CLOSED event fired');
    });
    page.on('crash', () => {
        // A renderer crash (most commonly an OOM kill — "Aw, Snap! Error
        // code 9" — when memory hits the task cap) leaves every subsequent
        // page.evaluate hanging on "Target crashed". Without this, the join
        // sequence stalls forever and the UI stays stuck on JOINING. Reject
        // the crash latch so the meeting init loses the race below and the
        // catch path drives a clean FAILED + task exit.
        console.error('[page-lifecycle] page CRASHED (renderer crash) — aborting join');
        rejectOnPageCrash(new Error('Renderer crashed (likely out of memory) — the meeting page died mid-join'));
    });
    page.on('framedetached', (frame) => {
        const isMain = frame === page.mainFrame();
        console.log(`[meeting-page] frame detached (mainFrame=${isMain}) url=${frame.url()}`);
    });
    context.on('close', () => {
        console.warn('[browser-lifecycle] browser context CLOSED');
    });

    // Initialize MCP command handler AFTER browser is launched and the
    // meeting page exists.
    if (statusManager && vpId) {
        try {
            const callId = process.env.VP_CALL_ID || '';
            if (callId) {
                mcpHandler = new MCPCommandHandler(vpId, callId);
                await mcpHandler.start();
                console.log('✓ MCP command handler started');
            } else {
                console.log('VP_CALL_ID not set - skipping MCP handler');
            }
        } catch (error) {
            console.error('Failed to start MCP command handler:', error);
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
                    console.log(`🔥 Strands warmup ping: ${payload.mcp_clients} MCP clients, from_cache=${payload.from_cache}, ${payload.warmup_time_ms}ms`);
                } catch (err) {
                    console.warn('Strands warmup ping failed (non-critical):', err);
                }
            };

            // Send initial warmup immediately to pre-warm before first user message
            sendWarmupPing();

            // Then send every 3 minutes to keep Lambda container and MCP connections alive
            strandsWarmupTimer = setInterval(sendWarmupPing, WARMUP_INTERVAL);
            console.log(`✓ Strands Lambda warmup timer started (every ${WARMUP_INTERVAL / 1000}s)`);
        } catch (error) {
            console.warn('Failed to start Strands warmup timer (non-critical):', error);
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
                console.log('Initializing Simli Avatar...');
                await simliAvatar.initialize(context);
                console.log('✓ Simli Avatar initialized');
            } catch (error) {
                console.error('Failed to initialize Simli Avatar (non-critical):', error);
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
                // 1. Grant camera+mic permissions at context level for all meeting domains
                for (const origin of ['https://zoom.us', 'https://app.zoom.us', 'https://app.chime.aws', 'https://teams.microsoft.com', 'https://web.webex.com']) {
                    await context.grantPermissions(['camera', 'microphone'], { origin }).catch(() => {});
                }
                console.log('✓ Camera and microphone permissions granted for meeting platforms');

                // 2. Inject the getUserMedia override AND start the Node-side
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
                console.log('✓ Simli getUserMedia override + WebRTC bridge installed');
            } catch (error) {
                console.error('Failed to set up Simli avatar for meeting (non-critical):', error);
            }
        })();
        return avatarPrepared;
    };

    let meeting: Chime | Zoom | ZoomSdk | Teams | TeamsSdk | Webex;
    let success = false;
    let exitInfo: ExitInfo | null = null;

    try {
        // Set JOINING status before attempting to join meeting
        if (statusManager) {
            await statusManager.setJoining();
            console.log(`VP ${vpId} status: JOINING`);
        }

        // Initialize the appropriate meeting platform handler
        console.log(`Initializing ${details.invite.meetingPlatform} handler...`);
        console.log(`DEBUG: Meeting platform value: "${details.invite.meetingPlatform}" (type: ${typeof details.invite.meetingPlatform})`);

        switch (details.invite.meetingPlatform) {
            case 'CHIME':
                meeting = new Chime();
                break;
            case 'ZOOM':
                meeting = details.zoomMethod === 'sdk' ? new ZoomSdk() : new Zoom();
                console.log(`Zoom join method: ${details.zoomMethod}`);
                break;
            case 'TEAMS':
            case 'Teams':
                meeting = details.teamsMethod === 'sdk' ? new TeamsSdk() : new Teams();
                console.log(`Teams join method: ${details.teamsMethod}`);
                break;
            case 'WEBEX':
                meeting = new Webex();
                break;
            default:
                throw new Error(`Unsupported meeting platform: ${details.invite.meetingPlatform}`);
        }

        // Start recording service
        recordingService.startRecording();

        // Start screen (video) recording in parallel with audio. No-op unless
        // ENABLE_VIDEO_RECORDING is set; runs independently of audio recording.
        try {
            await videoRecorder.start(process.env.VP_CALL_ID || '');
        } catch (error) {
            console.error('Error starting video recording:', error);
        }

        // Join the meeting and wait for it to end. The platform handler
        // returns a structured ExitInfo describing WHY the meeting ended;
        // we log it canonically and persist a human-readable form alongside
        // the COMPLETED status for the UI.
        // Race against the renderer-crash latch so an OOM crash mid-join
        // rejects here (→ catch → FAILED) instead of hanging forever on a
        // dead "Target crashed" page.evaluate.
        exitInfo = await Promise.race([
            meeting.initialize(page, { prepareAvatar }),
            pageCrashLatch,
        ]);

        const exitDetailParts = [
            `reason=${exitInfo.reason}`,
            `trigger=${exitInfo.trigger ?? 'n/a'}`,
            exitInfo.requestedBy ? `requestedBy=${JSON.stringify(exitInfo.requestedBy)}` : null,
            exitInfo.matchedMessage ? `message=${JSON.stringify(exitInfo.matchedMessage)}` : null,
        ].filter(Boolean);
        console.log(`VP exit: ${exitDetailParts.join(' ')}`);
        console.log('Meeting session completed successfully');
        success = true;


    } catch (error: any) {
        console.error('Meeting failed:', error.message);
        
        if (statusManager) {
            const errorMsg = error.message.toLowerCase();
            if (errorMsg.includes('renderer crashed') || errorMsg.includes('target crashed')) {
                await statusManager.setFailed('The meeting browser ran out of memory and crashed during join. Please try again.');
            } else if (errorMsg.includes('password') || errorMsg.includes('passcode')) {
                await statusManager.setFailed('Wrong meeting password');
            } else if (errorMsg.includes('meeting not found') || errorMsg.includes('invalid meeting')) {
                await statusManager.setFailed('Invalid meeting ID');
            } else if (errorMsg.includes('meeting ended') || errorMsg.includes('meeting has ended')) {
                await statusManager.setFailed('Meeting already ended');
            } else if (errorMsg.includes('permission denied') || errorMsg.includes('not authorized')) {
                await statusManager.setFailed('Permission denied');
            } else {
                await statusManager.setFailed(`Meeting join failed: ${error.message}`);
            }
        }
        
    } finally {
        // Cleanup - set flag to prevent uncaughtException from killing process mid-cleanup
        cleanupInProgress = true;
        console.log('Cleaning up...');
        
        try {
            // Stop transcription service
            await transcriptionService.stopTranscription();
        } catch (error) {
            console.error('Error stopping transcription:', error);
        }

        try {
            // Stop recording and upload to S3
            const recordingUrl = await recordingService.cleanup();
            if (recordingUrl) {
                console.log(`Recording uploaded: ${recordingUrl}`);
                // Send separate recording URL event (matching Python)
                const { kinesisStreamManager } = await import('./kinesis-stream.js');
                await kinesisStreamManager.sendCallRecording(recordingUrl);
            }

            // Stop screen (video) recording and upload to S3. Independent of
            // audio: a video failure must not block the END event below.
            try {
                const videoRecordingUrl = await videoRecorder.stop();
                if (videoRecordingUrl) {
                    console.log(`Video recording uploaded: ${videoRecordingUrl}`);
                    const { kinesisStreamManager } = await import('./kinesis-stream.js');
                    await kinesisStreamManager.sendCallVideoRecording(videoRecordingUrl);
                }
            } catch (videoError) {
                console.error('Error handling video recording cleanup:', videoError);
            }

            // Always send END event
            await sendEndMeeting();
        } catch (error) {
            console.error('Error handling recording cleanup:', error);
        }

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
            console.log(`Waiting up to ${HARD_CAP_MS / 1000}s for user to acknowledge failure before tearing down VNC...`);
            while (Date.now() < deadline) {
                if (await statusManager.getUserAcknowledgedFailure()) {
                    console.log('✓ User acknowledged failure — proceeding with tear-down');
                    break;
                }
                await new Promise((r) => setTimeout(r, POLL_MS));
            }
            if (Date.now() >= deadline) {
                console.log('Failure-ack hard cap reached — proceeding with tear-down');
            }
        }

        // Deregister from ALB target group
        if (statusManager) {
            try {
                await statusManager.deregisterFromTargetGroup();
                console.log('✓ Deregistered from ALB target group');
            } catch (error) {
                console.error('Error deregistering from ALB:', error);
            }
        }

        // Stop Strands warmup timer
        if (strandsWarmupTimer) {
            clearInterval(strandsWarmupTimer);
            strandsWarmupTimer = null;
            console.log('✓ Strands warmup timer stopped');
        }

        // Stop MCP handler
        if (mcpHandler) {
            try {
                await mcpHandler.stop();
                console.log('✓ MCP handler stopped');
            } catch (error) {
                console.error('Error stopping MCP handler:', error);
            }
        }

        // Stop Simli Avatar
        try {
            await simliAvatar.stop();
            console.log('✓ Simli Avatar stopped');
        } catch (error) {
            console.error('Error stopping Simli Avatar:', error);
        }

        // Stop agent speaking detector
        try {
            agentSpeakingDetector.stop();
        } catch (error) {
            console.error('Error stopping AgentSpeakingDetector:', error);
        }

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
        const joined = exitInfo ? didJoinMeeting(exitInfo) : true;
        if (success && joined) {
            const completionMessage = exitInfo ? formatExitMessage(exitInfo) : undefined;
            if (statusManager) {
                await statusManager.setCompleted(completionMessage);
            }
            console.log(
                completionMessage
                    ? `LMA Virtual Participant completed: ${completionMessage}`
                    : 'LMA Virtual Participant completed successfully',
            );
            process.exit(0);
        } else if (success && !joined) {
            // Ran cleanly but never joined → FAILED with the reason.
            const failMessage = exitInfo ? formatExitMessage(exitInfo) : 'Could not join the meeting.';
            if (statusManager) {
                await statusManager.setFailed(failMessage);
            }
            console.log(`LMA Virtual Participant did not join: ${failMessage}`);
            process.exit(1);
        } else {
            console.log('LMA Virtual Participant failed');
            process.exit(1);
        }
        
        console.log('Ending Task. Bye.');
        process.exit(1);
    }
};

// Handle process signals for graceful shutdown 
const signalHandler = async (signal: string) => {
    console.log(`Received ${signal}, initiating graceful shutdown...`);
    shutdownRequested = true;
    
    // Deregister from ALB target group
    if (statusManager) {
        try {
            await statusManager.deregisterFromTargetGroup();
            console.log('✓ Deregistered from ALB target group');
        } catch (error) {
            console.error('Error deregistering from ALB:', error);
        }
    }
    
    // Send END event to Kinesis when externally terminated
    try {
        console.log('Sending END meeting event due to external termination...');
        await sendEndMeeting();
        console.log('END meeting event sent successfully');
    } catch (error) {
        console.error(`Failed to send END meeting event: ${error}`);
    }
    
    // Update status to COMPLETED when externally terminated
    if (statusManager && vpId) {
        try {
            await statusManager.setCompleted(); // Use COMPLETED for external termination
            console.log(`VP ${vpId} status updated to COMPLETED due to external termination`);
        } catch (error) {
            console.error(`Failed to update status during shutdown: ${error}`);
        }
    }
    
    // Stop Strands warmup timer
    if (strandsWarmupTimer) {
        clearInterval(strandsWarmupTimer);
        strandsWarmupTimer = null;
    }

    // Stop MCP handler
    if (mcpHandler) {
        try {
            await mcpHandler.stop();
        } catch (error) {
            console.error('Error stopping MCP handler:', error);
        }
    }
    
    // Stop Simli Avatar
    try {
        await simliAvatar.stop();
    } catch (error) {
        console.error('Error stopping Simli Avatar:', error);
    }

    // Stop services
    try {
        await transcriptionService.stopTranscription();
        const recordingUrl = await recordingService.cleanup();
        if (recordingUrl) {
            console.log(`Final recording uploaded: ${recordingUrl}`);
        }
    } catch (error) {
        console.error('Error during service cleanup:', error);
    }

    // Close the browser and flush the profile to S3 so the session is
    // remembered next launch. nodemon/Ctrl-C deliver SIGTERM/SIGINT here, so
    // without this the latest session would never be persisted.
    await closeAndPersistProfile();

    console.log('Graceful shutdown complete. Exiting...');
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
    console.log('Performing emergency cleanup before exit...');

    // Send END event to Kinesis so the meeting is marked as ended
    try {
        console.log('Emergency: Sending END meeting event to Kinesis...');
        await sendEndMeeting();
        console.log('Emergency: END meeting event sent successfully');
    } catch (endError) {
        console.error('Emergency: Failed to send END meeting event:', endError);
    }

    // Update VP status to FAILED so it doesn't show as in-progress
    if (statusManager) {
        try {
            console.log('Emergency: Updating VP status to FAILED...');
            await statusManager.setFailed(errorMessage);
            console.log('Emergency: VP status updated to FAILED');
        } catch (statusError) {
            console.error('Emergency: Failed to update VP status:', statusError);
        }

        // Deregister from ALB target group
        try {
            await statusManager.deregisterFromTargetGroup();
            console.log('Emergency: Deregistered from ALB target group');
        } catch (albError) {
            console.error('Emergency: Failed to deregister from ALB:', albError);
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

    console.log('Emergency cleanup complete');
};

// Handle uncaught exceptions - always attempt cleanup before exiting
process.on('uncaughtException', async (error: any) => {
    if (cleanupInProgress) {
        // During cleanup, log but don't exit - let the finally block complete
        console.error('Uncaught Exception during cleanup (non-fatal):', error.message || error);
    } else {
        console.error('Uncaught Exception:', error);
        // Perform emergency cleanup before exiting to ensure meeting status is updated
        // and the meeting doesn't stay stuck as "in progress" in LMA
        await emergencyCleanup('Uncaught exception: ' + (error.message || error));
        process.exit(1);
    }
});

process.on('unhandledRejection', async (reason: any, promise: any) => {
    if (cleanupInProgress) {
        // During cleanup, log but don't exit - let the finally block complete
        console.error('Unhandled Rejection during cleanup (non-fatal):', reason);
    } else {
        console.error('Unhandled Rejection at:', promise, 'reason:', reason);
        // Perform emergency cleanup before exiting to ensure meeting status is updated
        await emergencyCleanup('Unhandled rejection: ' + (reason?.message || reason));
        process.exit(1);
    }
});

// Start the application
main().catch(async (error) => {
    console.error('Application failed to start:', error);
    await emergencyCleanup('Application startup failed: ' + (error.message || error));
    process.exit(1);
});
