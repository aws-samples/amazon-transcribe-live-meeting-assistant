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

import { BrowserContext, Page, Frame, Route } from 'playwright-core';
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
  
  private wsServer: any = null; // WebSocket.Server
  private wsClient: any = null; // Active WebSocket connection from Simli page
  private wsPort: number = 0;

  // Simli tears down the session after maxIdleTime (default 300s) with no audio
  // input. Nova only sends audio while speaking, so in a quiet meeting the
  // avatar goes black at ~5 min. We feed Simli silent PCM on a keepalive timer
  // whenever no real audio has flowed recently, resetting its idle timer.
  private lastAudioSentAt: number = 0;
  private keepAliveTimer: NodeJS.Timeout | null = null;

  // Watchdog that rebuilds the Simli LiveKit session if it drops mid-meeting
  // (the SDK does not self-heal — a drop otherwise freezes the avatar for the
  // rest of the meeting). When the session is rebuilt, the canvas source goes
  // live again and the existing WebRTC bridge keeps forwarding frames.
  private reconnectTimer: NodeJS.Timeout | null = null;
  private reconnectInFlight: boolean = false;

  // Video transport: page-to-page WebRTC bridge (Simli page → meeting frame),
  // platform-agnostic (Zoom/Teams/Chime/Webex all funnel through getUserMedia).
  //
  // cloakbrowser breaks page.exposeFunction (invocation throws), so we CANNOT
  // call back into Node from page code. Instead Node orchestrates everything
  // via evaluate over a polling rendezvous:
  //   - The in-frame getUserMedia override sets window.__simliWantsCamera and
  //     awaits window.__simliVideoTrack (it never creates a peer connection).
  //   - A Node poll loop scans page.frames(); for any frame that wants the
  //     camera, it runs the offer/answer handshake — creating the RECEIVER PC
  //     IN THAT FRAME via frame.evaluate(() => window.__simliAcceptOffer(...)).
  //     A MediaStreamTrack is bound to the realm that created it, so the
  //     receiver MUST live in the same frame that called getUserMedia (Zoom et
  //     al. may capture from a subframe).
  //   - The offerer PC lives on the Simli page (canvas.captureStream source).
  // cloakbrowser's WebRTC IP-leak patch suppresses ICE candidates by default,
  // so this only works with --force-webrtc-ip-handling-policy=default +
  // --webrtc-ip-handling-policy=default in the launch args (see index.ts).
  private bridgeSeq: number = 0;
  private bridgePollTimer: NodeJS.Timeout | null = null;
  private bridgePollStopped: boolean = false;
  private bridgeInFlight: Set<Frame> = new Set();

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
      
      // Define a re-runnable connect routine on the page so the Node watchdog
      // can rebuild the Simli session if it drops mid-meeting. The SimliClient's
      // stop/error/startup_error events only flip __simliReady=false — they do
      // NOT self-heal — so without this the avatar freezes for the rest of the
      // meeting when the LiveKit session dies (Simli maxIdleTime, server
      // teardown, network hiccup).
      await this.simliPage.evaluate((config: { apiKey: string; faceId: string; maxSessionLength: number; maxIdleTime: number; transportMode: string }) => {
        const w = window as any;
        w.__simliConnecting = false;

        w.__simliConnect = async (): Promise<{ success: boolean; error?: string }> => {
          if (w.__simliConnecting) return { success: false, error: 'already-connecting' };
          w.__simliConnecting = true;
          try {
            // @ts-ignore
            const { SimliClient, generateSimliSessionToken } = window.SimliModule;
            const videoElement = document.getElementById('simli-video') as HTMLVideoElement;
            const audioElement = document.getElementById('simli-audio') as HTMLAudioElement;
            if (!videoElement || !audioElement) throw new Error('Video or audio elements not found');

            // Tear down any prior (dead) client before rebuilding.
            if (w.__simliClient) {
              try {
                const old = w.__simliClient;
                if (typeof old.close === 'function') old.close();
                else if (typeof old.stop === 'function') old.stop();
              } catch (e) { /* ignore */ }
              w.__simliClient = null;
            }

            const sessionToken = await generateSimliSessionToken({
              apiKey: config.apiKey,
              config: {
                faceId: config.faceId,
                handleSilence: true,
                maxSessionLength: config.maxSessionLength,
                maxIdleTime: config.maxIdleTime,
              },
            });

            // new SimliClient(session_token, video, audio, iceServers|null, logLevel, transport_mode)
            const simliClient = new SimliClient(
              sessionToken.session_token,
              videoElement,
              audioElement,
              null,
              'DEBUG',
              config.transportMode,
            );
            w.__simliClient = simliClient;

            simliClient.on('start', () => {
              console.log('[Simli] Avatar connected and visible');
              w.__simliReady = true;
            });
            simliClient.on('stop', (reason: string) => {
              console.log('[Simli] Connection stopped:', reason, '— watchdog will reconnect');
              w.__simliReady = false;
            });
            simliClient.on('error', (reason: string) => {
              console.error('[Simli] Error:', reason);
            });
            simliClient.on('startup_error', (reason: string) => {
              console.error('[Simli] Startup error:', reason);
              w.__simliReady = false;
            });
            simliClient.on('speaking', () => console.log('[Simli] Avatar speaking'));
            simliClient.on('silent', () => console.log('[Simli] Avatar silent'));

            await simliClient.start();
            return { success: true };
          } catch (error: any) {
            return { success: false, error: error.message || String(error) };
          } finally {
            w.__simliConnecting = false;
          }
        };
      }, {
        apiKey: this.apiKey,
        faceId: this.faceId,
        maxSessionLength: this.maxSessionLength,
        maxIdleTime: this.maxIdleTime,
        transportMode: this.transportMode,
      });

      const initResult = await this.simliPage.evaluate(async () => {
        // @ts-ignore
        return await (window as any).__simliConnect();
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

      // Set up the avatar capture source on the Simli page. We draw the
      // <video> onto a canvas via setInterval (NOT requestAnimationFrame,
      // which Chromium pauses in background tabs) and expose
      // canvas.captureStream() as window.__simliCanvasStream. Each WebRTC
      // bridge created later adds this stream's video track to its sender.
      await this.simliPage.evaluate(() => {
        const video = document.getElementById('simli-video') as HTMLVideoElement;
        const canvas = document.getElementById('simli-canvas') as HTMLCanvasElement;
        const ctx = canvas.getContext('2d');
        if (!video || !canvas || !ctx) {
          console.error('[Simli] Cannot set up canvas capture - elements missing');
          return;
        }
        // Downscale the bridged avatar to keep the WebRTC encode + canvas
        // compositing cheap. The whole video pipeline runs on the CPU (no
        // hardware GPU; --disable-gpu), so 512x512@30 was a needless cost. A
        // talking-head avatar is fine at 256x256@~15fps, which roughly quarters
        // the per-frame encode/readback work.
        const CAP_SIZE = 256;
        const CAP_FPS = 15;
        canvas.width = CAP_SIZE;
        canvas.height = CAP_SIZE;

        // Keep the source <video> playing. It's a background tab and the
        // element is re-fed on every Simli reconnect; if it ever pauses/stalls,
        // the canvas would re-draw the same frame forever (frozen avatar). Re-
        // issue play() on the events that indicate it stopped advancing.
        const tryPlay = () => { video.play?.().catch(() => { /* autoplay/idle */ }); };
        tryPlay();
        ['pause', 'stalled', 'waiting', 'emptied', 'loadedmetadata', 'canplay'].forEach((ev) =>
          video.addEventListener(ev, tryPlay),
        );

        let frameCount = 0;
        let lastTime = -1;
        let frozenTicks = 0;
        setInterval(() => {
          if (video.readyState >= 2) {
            // Detect a frozen source so logs report the truth (currentTime not
            // advancing) rather than a healthy-looking frameCount on stale pixels.
            if (video.currentTime !== lastTime) { lastTime = video.currentTime; frozenTicks = 0; }
            else if (++frozenTicks === 30) { // ~2s stuck at 15fps
              console.warn(`[Simli] source video FROZEN: currentTime stuck at ${video.currentTime}`);
              tryPlay();
            }
            // Scale the source into the fixed downscaled canvas.
            ctx.drawImage(video, 0, 0, CAP_SIZE, CAP_SIZE);
            frameCount++;
            if (frameCount % 150 === 0) {
              console.log(`[Simli] Canvas drawing: frame=${frameCount}, t=${video.currentTime.toFixed(1)}, src=${video.videoWidth}x${video.videoHeight} cap=${CAP_SIZE}@${CAP_FPS}`);
            }
          }
        }, Math.round(1000 / CAP_FPS));
        const canvasStream = canvas.captureStream(CAP_FPS);
        // @ts-ignore
        window.__simliCanvasStream = canvasStream;
        console.log(`[Simli] Canvas capture stream created: ${canvasStream.getVideoTracks().length} video track(s)`);
      });
      console.log('✓ Simli page canvas capture ready for WebRTC bridge');

      this._isConnected = true;
      this._isReady = true;
      this.lastAudioSentAt = Date.now();
      this.startKeepAlive();
      this.startReconnectWatchdog();
      console.log('✓ Simli Avatar initialized successfully (audio isolated, WebSocket audio bridge + WebRTC video bridge active)');

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

      // Install the getUserMedia override in EVERY frame (main doc + Zoom's
      // about:blank / reCAPTCHA iframes, Teams/Webex/Chime subframes). The
      // override does NOT create any peer connection or call back into Node
      // (exposeFunction is broken under cloakbrowser). Instead it sets a
      // per-frame "wants camera" flag and awaits window.__simliVideoTrack,
      // which the Node poll loop fills by running the WebRTC handshake IN THIS
      // FRAME (see startBridgePollLoop + __simliAcceptOffer below).
      await meetingPage.addInitScript(() => {
        console.log(`[LMA-Simli] init script running in frame: ${location.href} (top=${window.top === window.self})`);
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          console.log('[LMA-Simli] navigator.mediaDevices.getUserMedia NOT available in this frame — override skipped');
          return;
        }
        const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);

        const waitIceComplete = (pc: RTCPeerConnection): Promise<void> =>
          new Promise((resolve) => {
            if (pc.iceGatheringState === 'complete') return resolve();
            pc.addEventListener('icegatheringstatechange', () => {
              if (pc.iceGatheringState === 'complete') resolve();
            });
          });

        // Per-frame bridge rendezvous state (read by the Node poll loop).
        // The loop builds a bridge for a frame iff wantsCamera && the source
        // track is not live — so it builds exactly once, and auto-rebuilds only
        // if the source dies. No per-call counter (that caused a runaway churn).
        const w = window as any;
        w.__simliWantsCamera = false; // set true on first getUserMedia({video})
        w.__simliVideoTrack = null;   // long-lived SOURCE track, set by __simliAcceptOffer's ontrack
        w.__simliReceiverPC = null;

        const isTrackUsable = (): boolean => {
          const t: MediaStreamTrack | null = w.__simliVideoTrack;
          return !!t && t.readyState === 'live';
        };

        // Called by Node via frame.evaluate(() => window.__simliAcceptOffer(sdp)).
        // Runs in THIS frame's realm so the resulting MediaStreamTrack is usable
        // by this frame's getUserMedia caller. Returns the answer SDP string.
        //
        // Builds the bridge ONCE per frame: the receiver track here is the
        // long-lived SOURCE. getUserMedia hands out clone()s of it (see below),
        // so when Zoom stops a clone (preview → meeting transition, camera
        // toggle) the source is untouched. We must NOT tear down a live source
        // PC on a re-request — doing so ended the track Zoom was displaying,
        // which made Zoom re-call getUserMedia, which rebuilt the bridge, which
        // ended the new track… a runaway loop that dropped the avatar.
        w.__simliAcceptOffer = async (offerSdp: string): Promise<string | null> => {
          try {
            // Source already live — Node shouldn't have called us, but guard.
            if (isTrackUsable()) return null;
            // Clean up a dead prior PC before rebuilding.
            if (w.__simliReceiverPC) {
              try { w.__simliReceiverPC.close(); } catch (e) { /* ignore */ }
              w.__simliReceiverPC = null;
              w.__simliVideoTrack = null;
            }
            const pc = new RTCPeerConnection();
            w.__simliReceiverPC = pc;
            pc.ontrack = (event: RTCTrackEvent) => {
              console.log('[LMA-Simli] Received avatar video track from Simli page');
              w.__simliVideoTrack = event.track;
            };
            await pc.setRemoteDescription(JSON.parse(offerSdp));
            const answer = await pc.createAnswer();
            await pc.setLocalDescription(answer);
            await waitIceComplete(pc);
            return JSON.stringify(pc.localDescription);
          } catch (e) {
            console.log('[LMA-Simli] __simliAcceptOffer error:', (e as Error)?.message || e);
            return null;
          }
        };

        const buildSimliStream = async (
          constraints: MediaStreamConstraints,
          videoTrack: MediaStreamTrack,
        ): Promise<MediaStream> => {
          // Hand out a CLONE so the caller stopping its track never ends the
          // shared source track (independent clones; stopping one is isolated).
          const clone = videoTrack.clone();
          // The avatar canvas is small (256x256) but callers such as the ACS
          // calling SDK request exact 1920x1080. A track whose real size never
          // satisfies the negotiated sender wedges after a few frames, so scale
          // the avatar into the requested geometry before handing it over.
          try {
            const want = (constraints.video || {}) as MediaTrackConstraints;
            // Only exact/min are hard requirements a sender will wedge on.
            // Honoring `ideal` too would drag previously-working callers (e.g.
            // Chime's ideal:1280) onto this path for no benefit.
            const pick = (v: unknown): number | undefined => {
              if (typeof v === 'number') return v;
              const r = v as ConstrainULongRange | undefined;
              return r ? (r.exact ?? r.min) : undefined;
            };
            const pickFps = (v: unknown): number | undefined => {
              if (typeof v === 'number') return v;
              const r = v as ConstrainDoubleRange | undefined;
              return r ? (r.exact ?? r.ideal ?? r.min ?? r.max) : undefined;
            };
            const targetW = pick(want.width);
            const targetH = pick(want.height);
            const settings = clone.getSettings();
            if (
              targetW && targetH &&
              (settings.width !== targetW || settings.height !== targetH)
            ) {
              const fps = pickFps(want.frameRate) || 15;
              const video = document.createElement('video');
              video.muted = true;
              video.playsInline = true;
              video.srcObject = new MediaStream([clone]);
              await video.play().catch(() => undefined);
              const canvas = document.createElement('canvas');
              canvas.width = targetW;
              canvas.height = targetH;
              const ctx = canvas.getContext('2d');
              if (ctx) {
                ctx.fillStyle = '#000';
                ctx.fillRect(0, 0, targetW, targetH);
                // setInterval (not requestAnimationFrame) so the draw loop keeps
                // running when the page is backgrounded/throttled.
                let timer = 0;
                // track.stop() does not fire 'ended', so self-terminate on a
                // dead track: the camera watchdog can call getUserMedia every
                // 10s, and a leaked 1080p draw loop per call would pile up.
                const draw = () => {
                  try {
                    if (scaledTrack && scaledTrack.readyState !== 'live') {
                      clearInterval(timer);
                      video.srcObject = null;
                      clone.stop();
                      return;
                    }
                    if (!video.videoWidth) return;
                    const scale = Math.min(targetW / video.videoWidth, targetH / video.videoHeight);
                    const dw = video.videoWidth * scale;
                    const dh = video.videoHeight * scale;
                    ctx.fillStyle = '#000';
                    ctx.fillRect(0, 0, targetW, targetH);
                    ctx.drawImage(video, (targetW - dw) / 2, (targetH - dh) / 2, dw, dh);
                  } catch { /* keep the loop alive */ }
                };
                timer = setInterval(draw, Math.max(1000 / fps, 33)) as unknown as number;
                const scaled = canvas.captureStream(fps);
                const scaledTrack = scaled.getVideoTracks()[0];
                scaledTrack.addEventListener('ended', () => clearInterval(timer));
                console.log(
                  `[LMA-Simli] scaling avatar ${settings.width}x${settings.height} -> ${targetW}x${targetH} @${fps}fps`,
                );
                if (constraints.audio) {
                  const audioStream = await originalGetUserMedia({ audio: constraints.audio });
                  const combined = new MediaStream([scaledTrack]);
                  audioStream.getAudioTracks().forEach((t) => combined.addTrack(t));
                  return combined;
                }
                return new MediaStream([scaledTrack]);
              }
            }
          } catch (e) {
            console.log('[LMA-Simli] scale-to-constraints failed, using raw track:', (e as Error)?.message || e);
          }
          if (constraints.audio) {
            const audioStream = await originalGetUserMedia({ audio: constraints.audio });
            const combinedStream = new MediaStream();
            combinedStream.addTrack(clone);
            audioStream.getAudioTracks().forEach((track) => combinedStream.addTrack(track));
            return combinedStream;
          }
          return new MediaStream([clone]);
        };

        navigator.mediaDevices.getUserMedia = async function(
          constraints?: MediaStreamConstraints,
        ): Promise<MediaStream> {
          console.log(
            '[LMA-Simli] getUserMedia called with constraints:',
            JSON.stringify(constraints),
          );

          if (constraints?.video) {
            // Fast path: source track already live — return a fresh clone with
            // no Node round-trip and no new peer connection.
            if (isTrackUsable()) {
              return buildSimliStream(constraints, w.__simliVideoTrack);
            }
            // No source yet: ask Node to build the bridge (once) and wait for
            // the source track to appear, then hand out a clone.
            w.__simliWantsCamera = true;
            const startTs = Date.now();
            const budgetMs = 12000;
            while (Date.now() - startTs < budgetMs) {
              if (isTrackUsable()) {
                console.log(`[LMA-Simli] ✓ Avatar track ready after ${Date.now() - startTs}ms`);
                return buildSimliStream(constraints, w.__simliVideoTrack);
              }
              await new Promise((r) => setTimeout(r, 50));
            }
            console.log('[LMA-Simli] ⚠️ Avatar bridge not ready after 12s — falling through to real camera');
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

      // Drive the per-frame WebRTC handshake from Node (no exposeFunction).
      this.startBridgePollLoop(meetingPage);

      console.log('✓ getUserMedia override injected into meeting page');
    } catch (error) {
      console.error('Failed to inject getUserMedia override:', error);
      throw error;
    }
  }

  /**
   * Node-side bridge orchestrator. Polls every frame of the meeting page; for
   * any frame whose getUserMedia override is asking for the camera
   * (__simliWantsCamera > __simliServedReq), runs the WebRTC handshake:
   *   1. createBridgeOffer()  — offerer PC on the Simli page (canvas source)
   *   2. frame.evaluate(__simliAcceptOffer) — receiver PC IN that frame
   *   3. applyBridgeAnswer()  — completes the offerer side
   * then marks the request served so the override resolves with the track.
   *
   * Platform-agnostic: there is no Zoom/Teams/etc-specific logic — any frame
   * that calls getUserMedia({video}) is serviced the same way.
   */
  private startBridgePollLoop(meetingPage: Page): void {
    if (this.bridgePollTimer) return;
    const simliPage = this.simliPage!;
    this.bridgePollStopped = false;

    // Frames that never request the camera — Zoom churns dozens of these
    // (recaptcha, ad/analytics iframes, blank shells). Skipping them avoids a
    // frame.evaluate round-trip per frame per tick.
    const isIrrelevantFrame = (url: string): boolean =>
      !url ||
      url === 'about:blank' ||
      url.startsWith('about:') ||
      url.includes('recaptcha') ||
      url.includes('doubleclick') ||
      url.includes('google.com/recaptcha') ||
      url.includes('/gtm.') ||
      url.includes('company-target') ||
      url.includes('fls.doubleclick');

    // Self-adjusting cadence: poll fast (200ms) while we still need to build a
    // bridge (join/preview/reconnect), then back off to 1s in steady state once
    // a live bridge exists — the slow poll just catches new frames or a dropped
    // source. This removes the constant per-frame eval storm across 10-40 Zoom
    // frames once the avatar is up.
    const FAST_MS = 200;
    const SLOW_MS = 1000;

    const tick = async () => {
      if (this.bridgePollStopped) return; // stopped
      let anyLiveBridge = false;
      let frames: Frame[] = [];
      try {
        frames = meetingPage.frames();
      } catch (e) {
        // page gone — reschedule slowly and bail
        if (!this.bridgePollStopped) this.bridgePollTimer = setTimeout(tick, SLOW_MS);
        return;
      }
      for (const frame of frames) {
        if (this.bridgeInFlight.has(frame)) continue;
        let url = '';
        try { url = frame.url(); } catch (e) { continue; }
        if (isIrrelevantFrame(url)) continue;

        let state: { installed: boolean; wants: boolean; hasLiveTrack: boolean } | null = null;
        try {
          state = await frame.evaluate(() => {
            const w = window as any;
            const t = w.__simliVideoTrack;
            return {
              installed: w.__simliOverrideInstalled === true,
              wants: w.__simliWantsCamera === true,
              hasLiveTrack: !!t && t.readyState === 'live',
            };
          });
        } catch (e) {
          continue; // frame detached / context destroyed — Zoom churns these constantly
        }
        if (state?.hasLiveTrack) anyLiveBridge = true;
        // Build the bridge only when the frame wants the camera and has no live
        // source track yet. Once the source is live, getUserMedia hands out
        // clones with no further handshakes — so this fires once per frame (and
        // re-fires only if the source track dies, i.e. needs reconnect).
        if (!state || !state.installed || !state.wants || state.hasLiveTrack) continue;

        this.bridgeInFlight.add(frame);
        const id = `bridge-${++this.bridgeSeq}`;
        try {
          const offer = await this.createBridgeOffer(simliPage, id);
          if (!offer) {
            // Avatar source not ready yet (Simli still connecting) — retry next poll.
            continue;
          }
          console.log(`[simli-bridge] building bridge for frame=${url || '(unknown)'} id=${id}`);
          const answer = await frame.evaluate(
            async (sdp: string) => {
              const w = window as any;
              if (typeof w.__simliAcceptOffer !== 'function') return null;
              return await w.__simliAcceptOffer(sdp);
            },
            offer,
          );
          if (!answer) {
            // Source already live (override guard) or accept failed — drop the
            // offerer PC we just created so it doesn't linger.
            await this.discardBridgeOffer(simliPage, id);
            continue;
          }
          await this.applyBridgeAnswer(simliPage, id, answer);
          anyLiveBridge = true;
        } catch (e: any) {
          console.log(`[simli-bridge] handshake failed (id=${id}): ${e?.message || e}`);
        } finally {
          this.bridgeInFlight.delete(frame);
        }
      }
      if (this.bridgePollStopped) return; // stopped during awaits
      this.bridgePollTimer = setTimeout(tick, anyLiveBridge ? SLOW_MS : FAST_MS);
    };

    // Kick off (assign a real handle so stop() and the guard above work).
    this.bridgePollTimer = setTimeout(tick, FAST_MS);
  }

  /**
   * Offerer side (Simli page): create a NEW RTCPeerConnection, add the avatar
   * canvas-capture video track, gather ICE, and return the offer SDP. The PC is
   * stashed in window.__simliBridgePCs keyed by id so applyBridgeAnswer can
   * complete it. Returns null if no live avatar source exists yet.
   */
  private async createBridgeOffer(simliPage: Page, id: string): Promise<string | null> {
    try {
      return await simliPage.evaluate(async (bridgeId: string) => {
        const sourceStream: MediaStream | undefined = (window as any).__simliCanvasStream;
        if (!sourceStream) return null;
        const videoTrack = sourceStream.getVideoTracks()[0];
        if (!videoTrack || videoTrack.readyState !== 'live') return null;

        const pc = new RTCPeerConnection();
        const registry = ((window as any).__simliBridgePCs ||= {});
        registry[bridgeId] = pc;
        // Self-clean: when the consumer stops using this track (preview →
        // meeting transition, camera toggle) the peer goes disconnected/
        // failed/closed — close the offerer so we don't leak live senders all
        // encoding the same canvas.
        pc.addEventListener('connectionstatechange', () => {
          if (pc.connectionState === 'disconnected' || pc.connectionState === 'failed' || pc.connectionState === 'closed') {
            try { pc.close(); } catch (e) { /* ignore */ }
            delete registry[bridgeId];
            console.log(`[Simli] Bridge ${bridgeId} closed (${pc.connectionState})`);
          }
        });
        pc.addTrack(videoTrack, sourceStream);
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await new Promise<void>((resolve) => {
          if (pc.iceGatheringState === 'complete') return resolve();
          pc.addEventListener('icegatheringstatechange', () => {
            if (pc.iceGatheringState === 'complete') resolve();
          });
        });
        return JSON.stringify(pc.localDescription);
      }, id);
    } catch (error: any) {
      console.error('[simli-bridge] offer failed:', error?.message || error);
      return null;
    }
  }

  /** Offerer side (Simli page): close + forget an offerer PC that won't be used. */
  private async discardBridgeOffer(simliPage: Page, id: string): Promise<void> {
    try {
      await simliPage.evaluate((bridgeId: string) => {
        const registry = (window as any).__simliBridgePCs || {};
        const pc: RTCPeerConnection | undefined = registry[bridgeId];
        if (pc) { try { pc.close(); } catch (e) { /* ignore */ } delete registry[bridgeId]; }
      }, id);
    } catch (e) { /* ignore */ }
  }

  /** Offerer side (Simli page): apply the consumer frame's answer SDP. */
  private async applyBridgeAnswer(simliPage: Page, id: string, answerSdp: string): Promise<void> {
    try {
      await simliPage.evaluate(
        async (args: { bridgeId: string; answer: string }) => {
          const pc: RTCPeerConnection | undefined = ((window as any).__simliBridgePCs || {})[args.bridgeId];
          if (!pc) return;
          await pc.setRemoteDescription(JSON.parse(args.answer));
          console.log(`[Simli] Bridge ${args.bridgeId} answer applied — peer connection established`);
        },
        { bridgeId: id, answer: answerSdp },
      );
    } catch (error: any) {
      console.error('[simli-bridge] answer failed:', error?.message || error);
    }
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
    // Track real audio so the keepalive timer only injects silence when idle.
    this.lastAudioSentAt = Date.now();
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

  /**
   * Keep the Simli session alive during quiet stretches. Simli closes the
   * session after maxIdleTime with no audio input (→ black avatar). When no
   * real audio has flowed for a few seconds, push a small silent PCM16 frame
   * straight to the audio WS so Simli's idle timer keeps resetting. Silence is
   * sent only when idle, so it never competes with the agent's real speech.
   */
  private startKeepAlive(): void {
    if (this.keepAliveTimer) return;
    // 16kHz mono PCM16 — 100ms of silence = 1600 samples * 2 bytes.
    const silence = Buffer.alloc(3200);
    const idleThresholdMs = 5000;
    this.keepAliveTimer = setInterval(() => {
      if (!this._isConnected) return;
      if (Date.now() - this.lastAudioSentAt < idleThresholdMs) return;
      if (this.wsClient && this.wsClient.readyState === WebSocket.OPEN) {
        try { this.wsClient.send(silence); } catch (e) { /* ignore */ }
      }
    }, 3000);
    console.log('🫀 Simli keepalive started (silent audio when idle to avoid maxIdleTime disconnect)');
  }

  /**
   * Watchdog: if the Simli LiveKit session drops (__simliReady goes false), the
   * SDK does not reconnect on its own — the avatar would freeze for the rest of
   * the meeting. Poll readiness and rebuild the session via the page-side
   * __simliConnect() routine when it's down. The WebRTC video bridge is
   * unaffected (it forwards the canvas, which goes live again post-reconnect).
   */
  private startReconnectWatchdog(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setInterval(async () => {
      if (!this._isConnected || !this.simliPage || this.reconnectInFlight) return;
      let ready = false;
      try {
        ready = await this.simliPage.evaluate(() => (window as any).__simliReady === true);
      } catch (e) {
        return; // page busy/navigating — try next tick
      }
      if (ready) return;

      this.reconnectInFlight = true;
      console.warn('⚠️  Simli session down (__simliReady=false) — reconnecting...');
      try {
        const res = await this.simliPage.evaluate(async () => {
          // @ts-ignore
          return await (window as any).__simliConnect();
        });
        if (res?.success) {
          this.lastAudioSentAt = Date.now();
          console.log('✓ Simli session reconnected');
        } else if (res?.error !== 'already-connecting') {
          console.warn(`⚠️  Simli reconnect failed: ${res?.error} — will retry`);
        }
      } catch (e: any) {
        console.warn('⚠️  Simli reconnect threw:', e?.message || e);
      } finally {
        this.reconnectInFlight = false;
      }
    }, 4000);
    console.log('🐕 Simli reconnect watchdog started');
  }

  async stop(): Promise<void> {
    if (!this.enabled) return;
    console.log('Stopping Simli Avatar...');
    
    // Stop WebSocket audio bridge + the WebRTC bridge poll loop + keepalive first
    this.stopAudioWebSocket();
    this.bridgePollStopped = true;
    if (this.bridgePollTimer) {
      clearTimeout(this.bridgePollTimer);
      this.bridgePollTimer = null;
    }
    if (this.keepAliveTimer) {
      clearInterval(this.keepAliveTimer);
      this.keepAliveTimer = null;
    }
    if (this.reconnectTimer) {
      clearInterval(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.bridgeInFlight.clear();

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
            // @ts-ignore - close any in-flight WebRTC bridge offerer PCs
            const pcs = window.__simliBridgePCs || {};
            for (const k of Object.keys(pcs)) {
              try { pcs[k].close(); } catch (e) { /* ignore */ }
            }
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

  /**
   * Compatibility shim. The previous bridge required callers (index.ts and
   * zoom.ts's post-join video watchdog) to explicitly (re)connect the avatar
   * stream into the meeting page. This implementation instead runs a Node-side
   * poll loop (started in injectGetUserMediaOverride) that scans page.frames()
   * and services any frame requesting the avatar camera — so the bridge
   * self-establishes and self-heals with no explicit call. Kept as a no-op so
   * existing call sites compile and harmlessly do nothing.
   */
  async connectStreamToMeetingPage(_meetingPage: Page): Promise<void> {
    /* no-op: the frame poll loop owns bridging now */
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
    #simli-canvas { display: none; width: 640px; height: 480px; }
    #status { position: absolute; top: 10px; left: 10px; color: #0f0; font-family: monospace; font-size: 12px; z-index: 100; }
  </style>
</head>
<body>
  <div id="status">Simli Avatar: Initializing...</div>
  <video id="simli-video" autoplay playsinline muted></video>
  <audio id="simli-audio" muted></audio>
  <canvas id="simli-canvas"></canvas>
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
