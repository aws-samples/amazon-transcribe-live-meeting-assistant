// rebrowser-puppeteer is a drop-in replacement for puppeteer that patches
// CDP at the runtime-binding level so pages can't detect Runtime.Evaluate /
// chrome.runtime.connect shims that puppeteer-extra-plugin-stealth had to
// add on top of vanilla Puppeteer. Strictly fewer fingerprint signals.
import puppeteer from 'rebrowser-puppeteer';
import { promises as fs } from 'fs';
import Chime from './chime.js';
import Zoom from './zoom.js';
import Teams from './teams.js';
import Webex from './webex.js';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';
import { VirtualParticipantStatusManager } from './status-manager.js';
import { recordingService } from './recording.js';
import { sendEndMeeting, sendStartMeeting } from './kinesis-stream.js';
import { MCPCommandHandler } from './mcp-command-handler.js';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { simliAvatar } from './simli-avatar.js';
import { voiceAssistant } from './voice-assistant.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { acquireProfile, persistProfile, releaseProfile } from './profile-store.js';

// Window dimensions configuration
const WINDOW_WIDTH = 1920;
const WINDOW_HEIGHT = 1000;

// Shared Puppeteer configuration. userDataDir is set per-launch so that
// a user-specific persisted profile (Phase C4) can be plumbed in when
// available. Cookies and "trusted device" markers persist across meetings
// and skip Zoom's bot-detection / reCAPTCHA on subsequent joins.
const getPuppeteerConfig = (userDataDir?: string) => ({
    headless: false, // Changed to false to show browser window in VNC
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    ignoreDefaultArgs: ['--mute-audio', '--enable-automation'],
    protocolTimeout: details.meetingTimeout,
    timeout: details.meetingTimeout,
    userDataDir: userDataDir || undefined,
    args: [
        `--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT+80}`,
        "--use-fake-ui-for-media-stream",
        // Use real PulseAudio device (agent_mic) instead of fake device
        // This allows Chromium to use the virtual microphone we created
        "--autoplay-policy=no-user-gesture-required",
        "--disable-blink-features=AutomationControlled",
        "--disable-notifications",
        "--disable-extensions",
        "--disable-crash-reporter",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--enable-logging",
        "--v=1",
        "--enable-logging=stderr",
        "--log-level=0",
        "--remote-debugging-port=9222", // Enable remote debugging for MCP
        // Suppress Chrome's "Save password?" / autofill bubbles that
        // would otherwise overlay Zoom's skip-for-now links and break
        // automation. Backed up by the managed policies in the
        // Dockerfile, but adding the launch flags too in case the
        // policies dir isn't picked up on some runtime configurations.
        "--disable-features=PasswordManagerOnboarding,AutofillEnableAccountWalletStorage,PasswordImport,PasswordsAccountStorage,Translate",
        "--password-store=basic",
        "--use-mock-keychain",
        "--no-first-run",
        "--no-default-browser-check",
    ],
});

// Global variables for graceful shutdown
let shutdownRequested = false;
let cleanupInProgress = false;
let statusManager: VirtualParticipantStatusManager | null = null;
let vpId: string | null = null;
let mcpHandler: MCPCommandHandler | null = null;
let strandsWarmupTimer: NodeJS.Timeout | null = null;

// Local testing mode - skip ALB registration and AppSync updates
const isLocalTest = process.env.LOCAL_TEST === 'true';

const main = async (): Promise<void> => {
    console.log('LMA Virtual Participant starting...');
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

    // Register with ALB target group and wait for healthy (skip in local test mode)
    if (statusManager && !isLocalTest) {
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
    }

    // Publish VNC endpoint via AppSync (only after ALB registration and health check)
    if (statusManager && !isLocalTest) {
        try {
            await statusManager.setVncReady();
            console.log('✓ VNC endpoint published via AppSync');
        } catch (error) {
            console.error('Failed to publish VNC endpoint:', error);
            // Non-critical - continue with meeting join
        }
    } else if (isLocalTest) {
        console.log('✓ Skipping AppSync VNC ready update (local test mode)');
    }

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

    // Acquire per-user persistent Chromium profile (Phase C4) when a
    // Cognito sub is available. Falls back to a fresh profile if the
    // bucket env var isn't set or the profile is locked by another task.
    if (statusManager) {
        await statusManager.setHydratingProfile();
    }
    const profileHandle = await acquireProfile({
        cognitoSub: process.env.LMA_USER_SUB || '',
        platform: details.invite.meetingPlatform,
    });

    // Launch Puppeteer browser with stealth plugin for all platforms
    if (statusManager) {
        await statusManager.setLaunchingBrowser();
    }

    // Stale Singleton* symlinks (and IndexedDB / leveldb LOCK files) from a
    // previous unclean exit will block re-launch with errors like:
    //   "Failed to create /tmp/...-Default/SingletonSocket: File exists"
    //   "[8081:0:1234567890] LOCK: Resource temporarily unavailable"
    // The persistent S3 profile makes this much more likely (we hydrate a
    // user-data dir that was last written by a different container) so we
    // proactively unlink the lock files at startup.
    if (profileHandle.enabled && profileHandle.localDir) {
        try {
            const profileDir = profileHandle.localDir;
            const stale = await fs.readdir(profileDir);
            const lockNames = stale.filter((n: string) => n.startsWith('Singleton'));
            for (const name of lockNames) {
                try {
                    await fs.unlink(`${profileDir}/${name}`);
                    console.log(`[profile-store] Removed stale Chrome lock: ${name}`);
                } catch { /* best-effort */ }
            }
            for (const sub of ['Default/Local Storage/leveldb', 'Default/IndexedDB']) {
                try {
                    const items = await fs.readdir(`${profileDir}/${sub}`, { recursive: true } as any);
                    for (const it of items as string[]) {
                        if (typeof it === 'string' && it.endsWith('LOCK')) {
                            try { await fs.unlink(`${profileDir}/${sub}/${it}`); } catch { /* best-effort */ }
                        }
                    }
                } catch { /* may not exist */ }
            }
        } catch (lockErr) {
            console.warn('[profile-store] Stale Chrome lock cleanup failed (continuing):', lockErr);
        }
    }

    console.log('Launching browser (rebrowser-puppeteer)...');
    const browser = await puppeteer.launch(
        getPuppeteerConfig(profileHandle.enabled ? profileHandle.localDir : undefined),
    );

    // Wait for Chrome DevTools to be fully ready
    await new Promise(resolve => setTimeout(resolve, 2000));
    console.log('✓ Chrome launched with remote debugging on port 9222');

    // Initialize Simli Avatar AFTER browser is launched (background page for avatar rendering)
    if (simliAvatar.isSimliEnabled()) {
        try {
            console.log('Initializing Simli Avatar...');
            await simliAvatar.initialize(browser);
            console.log('✓ Simli Avatar initialized');
        } catch (error) {
            console.error('Failed to initialize Simli Avatar (non-critical):', error);
            // Non-fatal - meeting can proceed without avatar
        }
    }

    // Create the meeting page BEFORE starting the MCP handler. The MCP
    // handler spawns chrome-devtools-mcp as a child process which connects
    // to Chrome's :9222 debug endpoint. With rebrowser-puppeteer, having
    // an external CDP client attached at the moment we call
    // browser.newPage() can deadlock the target-creation handshake. Also
    // setting the user-agent here so the meeting page is fully prepared
    // before the avatar / mcp / meeting-handler chain runs.
    const page = await browser.newPage();
    await page.setViewport({ width: WINDOW_WIDTH, height: WINDOW_HEIGHT });
    page.setDefaultTimeout(20000);
    await page.setUserAgent(
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );

    // Forward meeting-page console output to container logs IMMEDIATELY so
    // logs from getUserMedia override / Simli bridge code (which runs before
    // the platform handler attaches its own listener) are captured. Also
    // forward any console.error so unhandled exceptions surface.
    page.on('console', (msg) => {
        const text = msg.text();
        const type = msg.type();
        if (
            text.includes('[LMA-Simli]') ||
            text.includes('[Simli]') ||
            type === 'error' ||
            type === 'warn'
        ) {
            console.log(`Browser ${type}: ${text}`);
        }
    });
    page.on('pageerror', (err) => console.warn('MeetingPage error:', err?.message || err));
    page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) {
            console.log(`[meeting-page] navigated → ${frame.url()}`);
        }
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

    // Simli Avatar: Inject getUserMedia override and set up stream connection
    // This must happen BEFORE the page navigates to the meeting URL
    if (simliAvatar.isConnected()) {
        try {
            // 1. Grant camera+mic permissions at browser level for all meeting domains
            const context = page.browser().defaultBrowserContext();
            for (const domain of ['https://zoom.us', 'https://app.zoom.us', 'https://app.chime.aws', 'https://teams.microsoft.com', 'https://web.webex.com']) {
                await context.overridePermissions(domain, ['camera', 'microphone']).catch(() => {});
            }
            console.log('✓ Camera and microphone permissions granted for meeting platforms');
            
            // 2. Inject getUserMedia/enumerateDevices/permissions overrides (evaluateOnNewDocument)
            await simliAvatar.injectGetUserMediaOverride(page);
            console.log('✓ Simli getUserMedia override injected into meeting page');
            
            let isReconnecting = false;
            let reconnectInFlight: Promise<void> | null = null;
            const connectSimliStream = async () => {
                try {
                    await simliAvatar.connectStreamToMeetingPage(page);
                    console.log('✓ Simli video stream connected to meeting page');
                } catch (error) {
                    console.error('Failed to connect Simli stream (non-critical):', error);
                }
            };

            await page.exposeFunction('__simliRequestReconnect', async () => {
                if (reconnectInFlight) {
                    await reconnectInFlight;
                    return;
                }
                isReconnecting = true;
                console.log('Simli avatar: on-demand reconnect requested from meeting page');
                reconnectInFlight = connectSimliStream().finally(() => {
                    reconnectInFlight = null;
                    isReconnecting = false;
                });
                await reconnectInFlight;
            });

            // Eagerly (re)build the WebRTC bridge whenever the meeting page
            // navigates to a meeting URL. The receiver PC + window.__setSimliVideoTrack
            // are scoped to a single document — every navigation tears them
            // down. Relying solely on the override's on-demand reconnect path
            // is fragile because (a) page.exposeFunction's persistence across
            // navigations is unreliable on rebrowser-puppeteer, and (b) Zoom
            // may decide it can't access the camera on first probe and not
            // call getUserMedia again. So we trigger the connect ourselves as
            // soon as we land on /wc/<meetingId>/join (or other meeting URLs).
            const isMeetingUrl = (u: string): boolean =>
                /\/wc\/\d+\/(join|start|live)/.test(u) ||
                /teams\.microsoft\.com\/.*meetup-join/.test(u) ||
                /web\.webex\.com\/meeting/.test(u) ||
                /chime\.aws\/meetings\//.test(u);
            page.on('framenavigated', async (frame) => {
                if (frame !== page.mainFrame()) return;
                const url = frame.url();
                if (!isMeetingUrl(url)) return;
                if (reconnectInFlight) return;
                console.log(`[simli-bridge] meeting URL detected (${url}) — building Simli WebRTC bridge`);
                reconnectInFlight = connectSimliStream().finally(() => {
                    reconnectInFlight = null;
                });
                await reconnectInFlight;
            });
            
        } catch (error) {
            console.error('Failed to set up Simli avatar for meeting (non-critical):', error);
        }
    }

    let meeting: Chime | Zoom | Teams | Webex;
    let success = false;

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
                meeting = new Zoom();
                break;
            case 'TEAMS':
            case 'Teams':
                meeting = new Teams();
                break;
            case 'WEBEX':
                meeting = new Webex();
                break;
            default:
                throw new Error(`Unsupported meeting platform: ${details.invite.meetingPlatform}`);
        }

        // Start recording service
        recordingService.startRecording();

        // Join the meeting and wait for it to end
        await meeting.initialize(page);
        
        console.log('Meeting session completed successfully');
        success = true;


    } catch (error: any) {
        console.error('Meeting failed:', error.message);
        
        if (statusManager) {
            const errorMsg = error.message.toLowerCase();
            if (errorMsg.includes('password') || errorMsg.includes('passcode')) {
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
            // Always send END event
            await sendEndMeeting();
        } catch (error) {
            console.error('Error handling recording cleanup:', error);
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

        try {
            // Close browser
            await browser.close();
        } catch (error) {
            console.error('Error closing browser:', error);
        }

        // Sync profile back to S3 and release the lock so the next launch
        // for this user can resume the session.
        try {
            await persistProfile(profileHandle);
        } catch (error) {
            console.error('Error persisting Chromium profile:', error);
        }
        try {
            await releaseProfile(profileHandle);
        } catch (error) {
            console.error('Error releasing Chromium profile:', error);
        }

        // Final status update
        if (success) {
            if (statusManager) {
                await statusManager.setCompleted();
            }
            console.log('LMA Virtual Participant completed successfully');
            process.exit(0);
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
