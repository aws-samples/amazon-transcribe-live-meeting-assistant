/**
 * Simli Avatar Manager
 * 
 * Manages a Simli avatar session that provides lip-synced video for the
 * Virtual Participant's camera feed. The avatar is driven by audio from
 * the voice assistant (Nova Sonic or ElevenLabs).
 * 
 * Architecture:
 * - A background Playwright page loads the Simli JS SDK via CDN
 * - Voice assistant audio (PCM16 16kHz) is forwarded to Simli via page.evaluate()
 * - Simli renders a lip-synced avatar video in a <video> element
 * - The meeting page's getUserMedia is overridden to return the Simli video stream
 * - Meeting participants see the animated avatar as the VP's camera
 * 
 * Audio isolation:
 *   Simli echoes audio back through its WebRTC connection. To prevent this from
 *   reaching PulseAudio's meeting_audio sink (which Nova monitors), we use two
 *   proven approaches:
 *   1. In-DOM muted audio element (muted=true, volume=0)
 *   2. AudioContext.connect() patch to block connections to AudioDestinationNode
 */

import { BrowserContext, Page, Route } from 'playwright-core';
import { WebSocketServer, WebSocket } from 'ws';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export interface SimliAvatarConfig {
  apiKey: string;
  faceId: string;
  maxSessionLength?: number;
  maxIdleTime?: number;
  transportMode?: 'livekit' | 'p2p';
}

export class SimliAvatar {
  private apiKey: string;
  private faceId: string;
  private maxSessionLength: number;
  private maxIdleTime: number;
  private transportMode: 'livekit' | 'p2p';
  private simliPage: Page | null = null;
  private _isConnected: boolean = false;
  private _isReady: boolean = false;
  private enabled: boolean;
  private audioChunkCount: number = 0;
  
  private wsServer: any = null; // WebSocket.Server (audio bridge: Nova → Simli)
  private wsClient: any = null; // Active WebSocket connection from Simli page
  private wsPort: number = 0;

  constructor(config: SimliAvatarConfig) {
    this.apiKey = config.apiKey || '';
    this.faceId = config.faceId || '';
    this.maxSessionLength = config.maxSessionLength || 3600;
    this.maxIdleTime = config.maxIdleTime || 300;
    this.transportMode = config.transportMode || 'livekit';
    this.enabled = !!(this.apiKey && this.faceId);

    if (this.enabled) {
      console.log('✓ Simli Avatar enabled');
      console.log(`  Face ID: ${this.faceId}`);
      console.log(`  Transport mode: ${this.transportMode}`);
      console.log(`  Max session length: ${this.maxSessionLength}s`);
      console.log(`  Max idle time: ${this.maxIdleTime}s`);
      console.log('  Video transport: loopback WebRTC bridge (native track)');
    } else {
      console.log('Simli Avatar disabled - no API key or Face ID provided');
    }
  }

  async initialize(context: BrowserContext): Promise<void> {
    if (!this.enabled) {
      console.log('Simli Avatar disabled - skipping initialization');
      return;
    }

    try {
      console.log('Initializing Simli Avatar...');

      this.simliPage = await context.newPage();

      // Forward browser-console output from the simli page to container logs
      // so [Simli] / [LMA-Simli] traces show up in CloudWatch and we can
      // diagnose stream / track issues end-to-end.
      this.simliPage.on('console', (msg) => {
        const text = msg.text();
        if (
          text.includes('[Simli]') ||
          text.includes('[LMA-Simli]') ||
          text.includes('[Simli-AudioBlock]')
        ) {
          console.log(`SimliPage ${msg.type()}: ${text}`);
        }
      });
      this.simliPage.on('pageerror', (err) => console.warn('SimliPage error:', err?.message || err));

      const simliPageHtml = this.buildSimliPageHtml();
      
      // AUDIO ISOLATION: Patch AudioNode.connect() BEFORE any scripts load.
      // This blocks audio from reaching the speakers (AudioDestinationNode)
      // while keeping the WebRTC connection alive for video rendering.
      await this.simliPage.addInitScript(() => {
        const origConnect = AudioNode.prototype.connect;
        AudioNode.prototype.connect = function(this: AudioNode, ...args: any[]) {
          const dest = args[0];
          if (dest instanceof AudioDestinationNode) {
            console.log('[Simli-AudioBlock] Blocked AudioNode connection to speakers');
            return dest;
          }
          return origConnect.apply(this, args as any);
        } as any;
        console.log('[Simli-AudioBlock] AudioContext patch installed');
      });

      // Serve both the page HTML and the simli-client bundle from a synthetic
      // http://local.simli/ origin. We goto() that origin instead of using
      // setContent(): Playwright's setContent() never resolves when the
      // injected HTML runs a module script that dynamic-import()s another
      // resource (the frame lifecycle waiter hangs). Routing + goto gives the
      // page a real same-origin context and a reliable load lifecycle.
      const simliBundlePath = path.resolve(__dirname, 'simli-client.bundle.mjs');
      await this.simliPage.route('http://local.simli/**', (route: Route) => {
        const url = route.request().url();
        try {
          if (url.endsWith('/simli-client.bundle.mjs')) {
            route.fulfill({
              status: 200,
              contentType: 'application/javascript',
              body: fs.readFileSync(simliBundlePath, 'utf-8'),
            });
          } else {
            // The page itself.
            route.fulfill({
              status: 200,
              contentType: 'text/html',
              body: simliPageHtml,
            });
          }
        } catch (e: any) {
          console.error('[Simli] route fulfill failed:', e?.message || e);
          route.abort();
        }
      });

      await this.simliPage.goto('http://local.simli/', { waitUntil: 'domcontentloaded' });

      // The page's module script dynamically imports the bundle into
      // window.SimliModule. Wait for that to resolve before using it.
      await this.simliPage.waitForFunction(
        // @ts-ignore - browser context
        () => (window as any).SimliModule !== undefined,
        undefined,
        { timeout: 30_000 },
      );

      // Prevent background tab throttling - Chromium throttles timers and
      // pauses requestAnimationFrame in background tabs. We need the Simli
      // page to keep rendering video even when the meeting tab is active.
      const cdpSession = await context.newCDPSession(this.simliPage);
      await cdpSession.send('Page.setWebLifecycleState', { state: 'active' });
      // Disable timer throttling for background tabs
      await cdpSession.send('Emulation.setFocusEmulationEnabled', { enabled: true });
      console.log('✓ Simli page loaded with audio isolation patch (background throttling disabled)');
      
      console.log('Waiting for Simli avatar to connect...');
      
      const initResult = await this.simliPage.evaluate(async (config: { apiKey: string; faceId: string; maxSessionLength: number; maxIdleTime: number; transportMode: string }) => {
        try {
          // simli-client v3.x exports SimliClient + generateSimliSessionToken.
          // @ts-ignore
          const { SimliClient, generateSimliSessionToken } = window.SimliModule;

          const videoElement = document.getElementById('simli-video') as HTMLVideoElement;
          // Use in-DOM muted audio element (tested and confirmed to block audio)
          const audioElement = document.getElementById('simli-audio') as HTMLAudioElement;

          if (!videoElement || !audioElement) {
            throw new Error('Video or audio elements not found');
          }

          // Step 1: get a session token from the Simli API.
          const sessionToken = await generateSimliSessionToken({
            apiKey: config.apiKey,
            config: {
              faceId: config.faceId,
              handleSilence: true,
              maxSessionLength: config.maxSessionLength,
              maxIdleTime: config.maxIdleTime,
            },
          });

          // Step 2: construct SimliClient (v3.x signature):
          //   new SimliClient(session_token, video, audio, iceServers|null, logLevel, transport_mode)
          // For livekit transport, iceServers should be null. For p2p, pass STUN/TURN config.
          const simliClient = new SimliClient(
            sessionToken.session_token,
            videoElement,
            audioElement,
            null,
            'DEBUG',
            config.transportMode,
          );

          // @ts-ignore
          window.__simliClient = simliClient;

          // simli-client v3.x events: start | stop | error | speaking | silent | startup_error
          simliClient.on('start', () => {
            console.log('[Simli] Avatar connected and visible');
            // @ts-ignore
            window.__simliReady = true;
          });
          simliClient.on('stop', (reason: string) => {
            console.log('[Simli] Connection stopped:', reason);
            // @ts-ignore
            window.__simliReady = false;
          });
          simliClient.on('error', (reason: string) => {
            console.error('[Simli] Error:', reason);
          });
          simliClient.on('startup_error', (reason: string) => {
            console.error('[Simli] Startup error:', reason);
            // @ts-ignore
            window.__simliReady = false;
          });
          simliClient.on('speaking', () => console.log('[Simli] Avatar speaking'));
          simliClient.on('silent', () => console.log('[Simli] Avatar silent'));

          await simliClient.start();
          return { success: true };
        } catch (error: any) {
          return { success: false, error: error.message || String(error) };
        }
      }, {
        apiKey: this.apiKey,
        faceId: this.faceId,
        maxSessionLength: this.maxSessionLength,
        maxIdleTime: this.maxIdleTime,
        transportMode: this.transportMode,
      });

      if (!initResult.success) {
        throw new Error(`Simli initialization failed: ${initResult.error}`);
      }

      let readyAttempts = 0;
      const maxReadyAttempts = 30;
      while (readyAttempts < maxReadyAttempts) {
        const isReady = await this.simliPage.evaluate(() => {
          // @ts-ignore
          return window.__simliReady === true;
        });
        if (isReady) break;
        await new Promise(resolve => setTimeout(resolve, 1000));
        readyAttempts++;
      }

      if (readyAttempts >= maxReadyAttempts) {
        console.warn('⚠️  Simli avatar did not become ready within timeout - continuing anyway');
      } else {
        console.log('✓ Simli avatar is ready and visible');
      }

      // Additional audio track disabling after connection (belt and suspenders)
      await this.simliPage.evaluate(() => {
        document.querySelectorAll('audio, video').forEach((el) => {
          const media = el as HTMLMediaElement;
          media.muted = true;
          media.volume = 0;
          if (media.srcObject && media.srcObject instanceof MediaStream) {
            media.srcObject.getAudioTracks().forEach(track => {
              track.enabled = false;
            });
          }
        });
        console.log('[Simli-AudioBlock] All media elements muted and audio tracks disabled');
      });

      // Start WebSocket audio bridge for efficient audio delivery
      await this.startAudioWebSocket();

      // Connect the Simli page to the WebSocket audio bridge
      // Note: This code runs in the browser context where WebSocket is the native browser API,
      // not the Node.js 'ws' module. We use @ts-ignore to avoid type conflicts.
      if (this.wsPort > 0) {
        await this.simliPage.evaluate((port: number) => {
          // @ts-ignore - Browser WebSocket, not Node.js ws module
          const ws = new WebSocket(`ws://127.0.0.1:${port}`);
          ws.binaryType = 'arraybuffer';
          
          ws.onopen = () => {
            console.log('[Simli-WS] Connected to audio bridge');
            // @ts-ignore
            window.__simliAudioWs = ws;
          };
          
          // @ts-ignore - Browser MessageEvent type
          ws.onmessage = (event: any) => {
            try {
              const bytes = new Uint8Array(event.data);
              // @ts-ignore
              const client = window.__simliClient;
              if (client) client.sendAudioData(bytes);
            } catch (error) {
              console.error('[Simli-WS] Error processing audio:', error);
            }
          };
          
          ws.onclose = () => {
            console.log('[Simli-WS] Audio bridge disconnected');
            // @ts-ignore
            window.__simliAudioWs = null;
          };
          
          // @ts-ignore - Browser Event type
          ws.onerror = (err: any) => {
            console.error('[Simli-WS] Audio bridge error');
          };
        }, this.wsPort);
        console.log('✓ Simli page connected to WebSocket audio bridge');
      }

      // NOTE: avatar VIDEO no longer uses a JPEG frame relay. It's bridged to
      // the meeting page as a native WebRTC track by connectStreamToMeetingPage
      // (loopback RTCPeerConnection), which is near-zero per-frame CPU. Only
      // the AUDIO bridge (Nova → Simli lip-sync) uses a WebSocket here.

      this._isConnected = true;
      this._isReady = true;
      console.log('✓ Simli Avatar initialized successfully (audio isolated, WebSocket audio bridge active)');

    } catch (error) {
      console.error('Failed to initialize Simli Avatar:', error);
      this._isConnected = false;
      this._isReady = false;
    }
  }

  async injectGetUserMediaOverride(meetingPage: Page): Promise<void> {
    if (!this.enabled || !this._isReady || !this.simliPage) {
      console.log('Simli Avatar not ready - skipping getUserMedia override');
      return;
    }

    try {
      console.log('Injecting getUserMedia override for Simli avatar...');

      await meetingPage.addInitScript(() => {
        // Installed in EVERY frame (Zoom captures the camera from a subframe).
        // The avatar video arrives as a NATIVE MediaStreamTrack over a
        // same-browser loopback RTCPeerConnection set up by Node
        // (connectStreamToMeetingPage): the Simli page is the sender, this page
        // is the receiver. pc.ontrack calls __setSimliVideoTrack with the live
        // track — no JPEG, no canvas, near-zero per-frame CPU. getUserMedia
        // returns a MediaStream wrapping a per-call clone of that track.
        console.log(`[LMA-Simli] init script running in frame: ${location.href} (top=${window.top === window.self})`);
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          console.log('[LMA-Simli] navigator.mediaDevices.getUserMedia NOT available in this frame — override skipped');
          return;
        }
        const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);

        // The bridge's pc.ontrack calls this with the live avatar track. We
        // keep the latest; getUserMedia clones it per call so Zoom stopping a
        // preview track never ends our source.
        let bridgeTrack: MediaStreamTrack | null = null;
        // @ts-ignore
        window.__setSimliVideoTrack = (track: MediaStreamTrack) => {
          bridgeTrack = track;
          // @ts-ignore
          window.__simliCurrentTrack = track;
          console.log(
            `[LMA-Simli] bridge track received: readyState=${track.readyState}, muted=${track.muted}`,
          );
        };
        const hasBridgeTrack = () =>
          !!bridgeTrack && bridgeTrack.readyState === 'live';

        // Mint the track to hand Zoom: a fresh clone of the bridge track so
        // Zoom calling track.stop() on the preview (preview→meeting handoff)
        // never ends our source.
        const mintVideoTrack = (): MediaStreamTrack | undefined => {
          if (!hasBridgeTrack()) return undefined;
          try {
            return bridgeTrack!.clone();
          } catch (e) {
            console.log('[LMA-Simli] bridge track clone failed, using original: ' + (e as Error).message);
            return bridgeTrack!;
          }
        };

        const buildSimliStream = async (
          constraints: MediaStreamConstraints,
        ): Promise<MediaStream> => {
          const videoTrack = mintVideoTrack();
          console.log(
            `[LMA-Simli] Returning Simli avatar stream — track readyState=${videoTrack?.readyState}`,
          );
          if (constraints.audio) {
            const audioStream = await originalGetUserMedia({ audio: constraints.audio });
            const combinedStream = new MediaStream();
            if (videoTrack) combinedStream.addTrack(videoTrack);
            audioStream.getAudioTracks().forEach((track) => combinedStream.addTrack(track));
            return combinedStream;
          }
          return new MediaStream(videoTrack ? [videoTrack] : []);
        };

        navigator.mediaDevices.getUserMedia = async function(
          constraints?: MediaStreamConstraints,
        ): Promise<MediaStream> {
          console.log(
            '[LMA-Simli] getUserMedia called with constraints:',
            JSON.stringify(constraints),
          );

          if (constraints?.video) {
            // Wait briefly for the bridge track so we never hand Zoom an empty
            // source (renders as a black/off camera tile).
            const startTs = Date.now();
            const budgetMs = 8000;
            while (!hasBridgeTrack() && Date.now() - startTs < budgetMs) {
              await new Promise((r) => setTimeout(r, 50));
            }
            if (hasBridgeTrack()) {
              console.log(`[LMA-Simli] ✓ Bridge track ready after ${Date.now() - startTs}ms`);
              return buildSimliStream(constraints);
            }
            console.log('[LMA-Simli] ⚠️ No bridge track after 8s — falling through');
          }

          console.log('[LMA-Simli] Falling through to original getUserMedia');
          return originalGetUserMedia(constraints);
        };

        // Override enumerateDevices to report a virtual camera device
        // Zoom checks this to determine if a camera is available
        const originalEnumerateDevices = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
        navigator.mediaDevices.enumerateDevices = async function(): Promise<MediaDeviceInfo[]> {
          const devices = await originalEnumerateDevices();
          // Add a virtual camera if not already present
          const hasVideoinput = devices.some(d => d.kind === 'videoinput');
          if (!hasVideoinput) {
            devices.push({
              deviceId: 'simli-avatar-camera',
              groupId: 'simli-group',
              kind: 'videoinput' as MediaDeviceKind,
              label: 'Simli Avatar Camera',
              toJSON: () => ({ deviceId: 'simli-avatar-camera', groupId: 'simli-group', kind: 'videoinput', label: 'Simli Avatar Camera' }),
            } as MediaDeviceInfo);
            console.log('[LMA-Simli] Added virtual camera to enumerateDevices');
          }
          return devices;
        };

        // Override Permissions API to always report camera as 'granted'
        // Zoom checks navigator.permissions.query({name: 'camera'}) 
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = async function(desc: PermissionDescriptor): Promise<PermissionStatus> {
          if (desc.name === 'camera') {
            console.log('[LMA-Simli] Permissions query for camera - returning granted');
            return { state: 'granted', onchange: null, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true } as any as PermissionStatus;
          }
          return originalQuery(desc);
        };

        // @ts-ignore
        window.__simliOverrideInstalled = true;
        console.log('[LMA-Simli] getUserMedia + enumerateDevices + permissions overrides installed');
      });

      console.log('✓ getUserMedia override injected into meeting page');
    } catch (error) {
      console.error('Failed to inject getUserMedia override:', error);
    }
  }

  /**
   * Bridge the Simli avatar's video into the meeting page via a same-browser
   * loopback RTCPeerConnection. The Simli page (sender) adds the avatar's
   * NATIVE video track (read straight off the <video>'s srcObject — no canvas,
   * no JPEG) and creates an offer; the meeting page (receiver) answers and its
   * pc.ontrack hands the live track to the getUserMedia override via
   * window.__setSimliVideoTrack. Frames never touch JS — near-zero per-frame
   * CPU. Both peers are in the same browser (loopback), and Simli's external
   * signaling stays on the local.simli origin, never exposed to Zoom's CSP.
   * Mirrors the pre-0.3.4 design that ran fine on t3.medium.
   *
   * Idempotent and safe to call on every meeting-URL navigation (each call
   * tears down any prior bridge PC and re-establishes). If it fails to connect
   * the avatar simply won't appear and the diagnostics below say why — there's
   * no JPEG-relay fallback (a single native path is the whole point).
   */
  async connectStreamToMeetingPage(meetingPage: Page): Promise<void> {
    if (!this.enabled || !this._isReady || !this.simliPage) return;
    try {
      const bridged = await this.bridgeVideoViaWebRTC(meetingPage);
      console.log(
        bridged
          ? '✓ Simli video bridged to meeting page via loopback WebRTC (native track).'
          : '⚠️  Simli video bridge did not connect — avatar will not appear (see diagnostics above).',
      );
    } catch (error) {
      console.error('[Simli] WebRTC bridge attempt threw:', error);
    }
  }

  /**
   * Loopback WebRTC offer/answer dance between the Simli page (sender) and the
   * meeting page (receiver). Node relays the SDP. Non-trickling ICE: each side
   * waits for ICE gathering to complete before handing its SDP back, so we
   * exchange one complete offer and one complete answer (simplest reliable
   * pattern for same-browser loopback). Returns true once the receiver reports
   * a connected PC with a live track.
   */
  private async bridgeVideoViaWebRTC(meetingPage: Page): Promise<boolean> {
    if (!this.simliPage) return false;

    // Sender: build offer from the avatar's native video track.
    const offer = await this.simliPage.evaluate(async () => {
      const videoEl = document.getElementById('simli-video') as HTMLVideoElement | null;
      let sourceStream: MediaStream | null = null;
      if (videoEl && videoEl.srcObject instanceof MediaStream) {
        const vts = videoEl.srcObject.getVideoTracks();
        if (vts.length > 0 && vts[0].readyState === 'live') {
          sourceStream = videoEl.srcObject;
          console.log(
            `[Simli] bridge sender using native srcObject track: ${vts[0].readyState}`,
          );
        }
      }
      if (!sourceStream) {
        console.log('[Simli] bridge sender: no live native track yet');
        return null;
      }
      // Close any prior bridge PC before re-creating.
      // @ts-ignore
      if (window.__simliPC) { try { window.__simliPC.close(); } catch (e) { /* ignore */ } }
      // No iceServers: this is same-browser loopback — host candidates only,
      // no STUN/TURN. Pairing relies on local-IP candidates being exposed
      // (see --force-webrtc-ip-handling-policy / disable mDNS in launch args).
      const pc = new RTCPeerConnection({ iceServers: [] });
      // @ts-ignore
      window.__simliPC = pc;
      const track = sourceStream.getVideoTracks()[0];
      pc.addTrack(track, sourceStream);
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === 'complete') return resolve();
        pc.addEventListener('icegatheringstatechange', () => {
          if (pc.iceGatheringState === 'complete') resolve();
        });
      });
      // DIAGNOSTIC: dump the ICE candidate lines from the gathered SDP so we
      // can see WHY loopback ICE fails (iceState stuck at "new"). Classify
      // each candidate's type (host/srflx) and whether the connection-address
      // is an mDNS .local hostname (unresolvable in the container → no
      // pairing) vs a real IP.
      const sdp = pc.localDescription?.sdp || '';
      const cands = sdp.split('\n').filter((l) => l.startsWith('a=candidate:'));
      const summary = cands.map((c) => {
        const parts = c.split(' ');
        const addr = parts[4] || '?';
        const typ = (c.match(/typ (\w+)/) || [])[1] || '?';
        const mdns = /\.local/.test(addr);
        return `${typ}:${mdns ? 'mdns' : addr}`;
      });
      console.log(`[Simli] bridge sender ICE candidates (${cands.length}): ${summary.join(', ') || 'NONE'}`);
      return JSON.stringify(pc.localDescription);
    });

    if (!offer) return false;

    // Receiver: answer in the main meeting frame, hand track to the override.
    const answer = await meetingPage.evaluate(async (offerStr: string) => {
      const offer = JSON.parse(offerStr);
      // @ts-ignore
      if (window.__simliReceiverPC) { try { window.__simliReceiverPC.close(); } catch (e) { /* ignore */ } }
      const pc = new RTCPeerConnection({ iceServers: [] });
      // @ts-ignore
      window.__simliReceiverPC = pc;
      pc.ontrack = (event: RTCTrackEvent) => {
        console.log('[LMA-Simli] bridge received video track from Simli page');
        // @ts-ignore
        if (typeof window.__setSimliVideoTrack === 'function') {
          // @ts-ignore
          window.__setSimliVideoTrack(event.track);
        }
      };
      await pc.setRemoteDescription(offer);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === 'complete') return resolve();
        pc.addEventListener('icegatheringstatechange', () => {
          if (pc.iceGatheringState === 'complete') resolve();
        });
      });
      return JSON.stringify(pc.localDescription);
    }, offer);

    // Sender: accept the answer.
    await this.simliPage.evaluate(async (answerStr: string) => {
      const answer = JSON.parse(answerStr);
      // @ts-ignore
      const pc = window.__simliPC as RTCPeerConnection;
      await pc.setRemoteDescription(answer);
      console.log('[Simli] bridge peer connection established with meeting page');
    }, answer);

    // Verify: receiver PC connected AND a live track is present.
    let connected = false;
    let lastDiag = '';
    for (let i = 0; i < 20; i++) {
      const diag = await meetingPage.evaluate(() => {
        // @ts-ignore
        const pc = window.__simliReceiverPC as RTCPeerConnection | undefined;
        // @ts-ignore
        const track = window.__simliCurrentTrack as MediaStreamTrack | undefined;
        return {
          pcState: pc?.connectionState ?? 'no-pc',
          iceState: pc?.iceConnectionState ?? 'no-pc',
          trackReady: track?.readyState ?? 'no-track',
        };
      });
      lastDiag = JSON.stringify(diag);
      if (
        (diag.pcState === 'connected' || diag.iceState === 'connected' || diag.iceState === 'completed') &&
        diag.trackReady === 'live'
      ) {
        connected = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    console.log(
      connected
        ? `[Simli] bridge verified connected (${lastDiag})`
        : `[Simli] bridge NOT connected after 10s (${lastDiag})`,
    );
    return connected;
  }

  private async startAudioWebSocket(): Promise<void> {
    return new Promise((resolve) => {
      // Use port 0 to let the OS assign a free port
      this.wsServer = new WebSocketServer({ port: 0, host: '127.0.0.1' });
      
      this.wsServer.on('listening', () => {
        const addr = this.wsServer.address();
        this.wsPort = typeof addr === 'object' ? addr.port : 0;
        console.log(`✓ Simli audio WebSocket server listening on ws://127.0.0.1:${this.wsPort}`);
        resolve();
      });

      this.wsServer.on('connection', (ws: WebSocket) => {
        console.log('✓ Simli page connected to audio WebSocket bridge');
        this.wsClient = ws;
        
        ws.on('close', () => {
          console.log('🔊 Simli audio WebSocket client disconnected');
          this.wsClient = null;
        });
        
        ws.on('error', (err: Error) => {
          console.error('❌ Simli audio WebSocket error:', err.message);
          this.wsClient = null;
        });
      });

      this.wsServer.on('error', (err: Error) => {
        console.error('❌ Simli audio WebSocket server error:', err);
        resolve(); // Don't block initialization
      });
    });
  }

  /**
   * Stop the WebSocket server.
   */
  private stopAudioWebSocket(): void {
    if (this.wsClient) {
      try { this.wsClient.close(); } catch (e) { /* ignore */ }
      this.wsClient = null;
    }
    if (this.wsServer) {
      try { this.wsServer.close(); } catch (e) { /* ignore */ }
      this.wsServer = null;
      console.log('✓ Simli audio WebSocket server stopped');
    }
  }

  /**
   * Clear the Simli avatar's audio buffer to stop lip-syncing on barge-in.
   * Calls SimliClient.ClearBuffer() — the official SDK method for stopping avatar speech.
   */
  async clearAudioBuffer(): Promise<void> {
    if (!this.enabled || !this._isConnected || !this.simliPage) return;

    console.log('🎭 Calling Simli ClearBuffer() to stop avatar lip-sync');
    try {
      await this.simliPage.evaluate(() => {
        // @ts-ignore
        const client = window.__simliClient;
        if (client) {
          if (typeof client.ClearBuffer === 'function') {
            client.ClearBuffer();
            console.log('[Simli] ClearBuffer() called successfully');
          } else if (typeof client.clearBuffer === 'function') {
            client.clearBuffer();
            console.log('[Simli] clearBuffer() called (legacy)');
          }
        }
      });
    } catch (err) {
      // Non-critical
    }
  }

  async sendAudioData(audioData: Buffer): Promise<void> {
    if (!this.enabled || !this._isConnected || !this.simliPage) return;

    this.audioChunkCount++;
    if (this.audioChunkCount % 100 === 0) {
      console.log(`🎭 Sent ${this.audioChunkCount} audio chunks to Simli avatar via ${this.wsClient ? 'WebSocket' : 'CDP fallback'}`);
    }

    // Primary path: WebSocket bridge (near-zero latency)
    if (this.wsClient && this.wsClient.readyState === WebSocket.OPEN) {
      try {
        this.wsClient.send(audioData);
        return;
      } catch (err) {
        // Fall through to CDP fallback
      }
    }

    // Fallback: page.evaluate() via CDP (slower but always works)
    try {
      const audioBase64 = audioData.toString('base64');
      this.simliPage.evaluate((base64Data: string) => {
        try {
          const binaryString = atob(base64Data);
          const bytes = new Uint8Array(binaryString.length);
          for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
          }
          // @ts-ignore
          const client = window.__simliClient;
          if (client) client.sendAudioData(bytes);
        } catch (error) {
          console.error('[Simli] Error sending audio data:', error);
        }
      }, audioBase64).catch(() => {
        // Silently ignore CDP errors
      });
    } catch (error) {
      if (this.audioChunkCount % 100 === 0) {
        console.error('Error sending audio to Simli:', error);
      }
    }
  }

  async stop(): Promise<void> {
    if (!this.enabled) return;
    console.log('Stopping Simli Avatar...');
    
    // Stop WebSocket audio bridge first
    this.stopAudioWebSocket();

    try {
      if (this.simliPage) {
        await this.simliPage.evaluate(() => {
          try {
            // @ts-ignore
            if (window.__simliAudioWs) window.__simliAudioWs.close();
            // @ts-ignore
            if (window.__simliClient) {
              // @ts-ignore - v3.x uses stop(); some legacy versions used close()
              const c = window.__simliClient;
              if (typeof c.stop === 'function') c.stop();
              else if (typeof c.close === 'function') c.close();
            }
            // @ts-ignore
            if (window.__simliPC) window.__simliPC.close();
          } catch (e) {
            console.error('[Simli] Error during cleanup:', e);
          }
        });
        await this.simliPage.close();
        this.simliPage = null;
      }
    } catch (error) {
      console.error('Error stopping Simli Avatar:', error);
    }
    this._isConnected = false;
    this._isReady = false;
    console.log('✓ Simli Avatar stopped');
  }

  isConnected(): boolean {
    return this._isConnected && this._isReady;
  }

  isSimliEnabled(): boolean {
    return this.enabled;
  }

  private buildSimliPageHtml(): string {
    return `<!DOCTYPE html>
<html>
<head>
  <title>Simli Avatar - LMA Virtual Participant</title>
  <style>
    body { margin: 0; padding: 0; background: #000; overflow: hidden; }
    #simli-video { width: 640px; height: 480px; object-fit: cover; }
    #simli-audio { display: none; }
    #status { position: absolute; top: 10px; left: 10px; color: #0f0; font-family: monospace; font-size: 12px; z-index: 100; }
  </style>
</head>
<body>
  <div id="status">Simli Avatar: Initializing...</div>
  <video id="simli-video" autoplay playsinline muted></video>
  <audio id="simli-audio" muted></audio>
  <script type="module">
    async function loadSimliClient() {
      try {
        const module = await import('http://local.simli/simli-client.bundle.mjs');
        window.SimliModule = module.default || module;
        document.getElementById('status').textContent = 'Simli Avatar: SDK Loaded';
        console.log('[Simli] SDK loaded successfully');
      } catch (error) {
        console.error('[Simli] Failed to load SDK:', error);
        document.getElementById('status').textContent = 'Simli Avatar: SDK Load Failed';
      }
    }
    loadSimliClient();
  </script>
</body>
</html>`;
  }
}

export function createSimliAvatarFromEnv(): SimliAvatar {
  return new SimliAvatar({
    apiKey: process.env.SIMLI_API_KEY || '',
    faceId: process.env.SIMLI_FACE_ID || '',
    maxSessionLength: parseInt(process.env.SIMLI_MAX_SESSION_LENGTH || '3600'),
    maxIdleTime: parseInt(process.env.SIMLI_MAX_IDLE_TIME || '300'),
    transportMode: (process.env.SIMLI_TRANSPORT_MODE as 'livekit' | 'p2p') || 'livekit',
  });
}

export const simliAvatar = createSimliAvatarFromEnv();
