import puppeteer from 'puppeteer';
import puppeteerExtra from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { promises as fs } from 'fs';
import Chime from './chime.js';
import Zoom from './zoom.js';
import Teams from './teams.js';
import Webex from './webex.js';
import { details } from './details.js';
import { transcriptionService } from './scribe.js';
import { VirtualParticipantStatusManager } from './status-manager.js';
import { recordingService } from './recording.js';
import { sendEndMeeting } from './kinesis-stream.js';

// Window dimensions configuration
const WINDOW_WIDTH = 1920;
const WINDOW_HEIGHT = 1080;

// Shared Puppeteer configuration
const getPuppeteerConfig = () => ({
    headless: false, // Changed to false to show browser window in VNC
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    ignoreDefaultArgs: ['--mute-audio'],
    protocolTimeout: details.meetingTimeout,
    timeout: details.meetingTimeout,
    args: [
        `--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}`,
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--disable-notifications",
        "--disable-extensions",
        "--disable-crash-reporter",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--enable-logging",
        "--v=1",
        "--enable-logging=stderr",
        "--log-level=0",
    ],
});

// Global variables for graceful shutdown
let shutdownRequested = false;
let statusManager: VirtualParticipantStatusManager | null = null;
let vpId: string | null = null;

const main = async (): Promise<void> => {
    console.log('LMA Virtual Participant starting...');
    console.log(`Meeting Platform: ${details.invite.meetingPlatform}`);
    console.log(`Meeting ID: ${details.invite.meetingId}`);
    console.log(`Meeting Name: ${details.invite.meetingName}`);
    console.log(`LMA User: ${details.lmaUser}`);
    console.log(`CallId: ${details.invite.meetingName}_${new Date().toISOString().replace(/[:.]/g, '-')}`);

    // Initialize status manager if VP_ID is provided
    vpId = details.invite.virtualParticipantId || null;
    if (vpId) {
        try {
            statusManager = new VirtualParticipantStatusManager(vpId);
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

    // Register with ALB target group and wait for healthy
    if (statusManager) {
        try {
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
    }

    // Publish VNC endpoint via AppSync (only after ALB registration and health check)
    if (statusManager) {
        try {
            await statusManager.setVncReady();
            console.log('✓ VNC endpoint published via AppSync');
        } catch (error) {
            console.error('Failed to publish VNC endpoint:', error);
            // Non-critical - continue with meeting join
        }
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

    // Launch Puppeteer browser
    console.log('Launching browser...');
    const isTeamsMeeting = details.invite.meetingPlatform === 'Teams' || details.invite.meetingPlatform === 'TEAMS';
    let browser;
    
    if (isTeamsMeeting) {
        console.log('DEBUG: Using puppeteer-extra with stealth plugin for Teams meeting');
        // Configure puppeteer-extra with stealth plugin for Teams
        puppeteerExtra.use(StealthPlugin());
        browser = await puppeteerExtra.launch(getPuppeteerConfig());
    } else {
        console.log('DEBUG: Using standard puppeteer for non-Teams meeting');
        browser = await puppeteer.launch(getPuppeteerConfig());
    }

    const page = await browser.newPage();
    await page.setViewport({ width: WINDOW_WIDTH, height: WINDOW_HEIGHT });
    page.setDefaultTimeout(20000);

    // Set user agent to avoid detection
    await page.setUserAgent(
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );

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

        // Join the meeting
        await meeting.initialize(page);
        
        console.log('Meeting joined successfully');
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
        // Cleanup
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

        try {
            // Close browser
            await browser.close();
        } catch (error) {
            console.error('Error closing browser:', error);
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

// Handle uncaught exceptions
process.on('uncaughtException', (error) => {
    console.error('Uncaught Exception:', error);
    process.exit(1);
});

process.on('unhandledRejection', (reason, promise) => {
    console.error('Unhandled Rejection at:', promise, 'reason:', reason);
    process.exit(1);
});

// Start the application
main().catch((error) => {
    console.error('Application failed to start:', error);
    process.exit(1);
});
