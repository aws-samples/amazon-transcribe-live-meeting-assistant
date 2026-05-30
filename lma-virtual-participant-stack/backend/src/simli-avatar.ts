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
  
  private wsServer: any = null; // WebSocket.Server
  private wsClient: any = null; // Active WebSocket connection from Simli page
  private wsPort: number = 0;

  // Video frame relay (replaces the tab-to-tab WebRTC bridge, which cloakbrowser's
  // WebRTC patches break). Simli page pushes JPEG frames up this WS; the meeting
  // page pulls the latest frame via the __simliPullFrame exposed binding.
  private videoWsServer: any = null;
  private videoWsPort: number = 0;
  private latestFrame: Buffer | null = null;

  // Video pipeline cost knobs. The avatar feed runs four serial per-frame
  // costs on the 2-vCPU host — producer JPEG encode, relay forward, consumer
  // JPEG decode, and Zoom's WebRTC re-encode of the captureStream — all
  // scaling with fps × pixels. At the old 20fps producer / 30fps capture /
  // native (~512²) / q0.6 the task pegged at ~98% CPU post-join, which froze
  // the avatar and starved the meeting UI. These defaults cut that load and
  // are env-overridable so they can be tuned without a code change.
  //   SIMLI_VIDEO_FPS         producer capture+encode rate   (default 12)
  //   SIMLI_CAPTURE_FPS       canvas→WebRTC captureStream fps (default 12)
  //   SIMLI_VIDEO_MAX_DIM     max frame dimension in px       (default 384)
  //   SIMLI_VIDEO_JPEG_QUALITY  0..1 JPEG quality             (default 0.5)
  private videoFps: number = SimliAvatar.envInt('SIMLI_VIDEO_FPS', 12, 1, 30);
  private captureFps: number = SimliAvatar.envInt('SIMLI_CAPTURE_FPS', 12, 1, 30);
  private videoMaxDim: number = SimliAvatar.envInt('SIMLI_VIDEO_MAX_DIM', 384, 128, 1280);
  private videoJpegQuality: number = SimliAvatar.envFloat('SIMLI_VIDEO_JPEG_QUALITY', 0.5, 0.2, 1.0);

  private static envInt(name: string, def: number, min: number, max: number): number {
    const v = parseInt(process.env[name] || '', 10);
    if (Number.isNaN(v)) return def;
    return Math.min(max, Math.max(min, v));
  }

  private static envFloat(name: string, def: number, min: number, max: number): number {
    const v = parseFloat(process.env[name] || '');
    if (Number.isNaN(v)) return def;
    return Math.min(max, Math.max(min, v));
  }

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
      console.log(
        `  Video: ${this.videoFps}fps producer / ${this.captureFps}fps capture / ` +
          `${this.videoMaxDim}px max / JPEG q${this.videoJpegQuality}`,
      );
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

      // Start video frame relay (Simli page → Node). The meeting page pulls
      // frames from Node instead of receiving a WebRTC track.
      await this.startVideoFrameRelay();
      
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

      // Simli page → Node video frame push: draw the avatar video to a canvas
      // and stream JPEG frames up the video WS. Mirror of the audio bridge.
      if (this.videoWsPort > 0) {
        await this.simliPage.evaluate(
          ({ port, fps, maxDim, quality }: { port: number; fps: number; maxDim: number; quality: number }) => {
          const video = document.getElementById('simli-video') as HTMLVideoElement;
          const canvas = document.createElement('canvas');
          const ctx = canvas.getContext('2d');
          if (!video || !ctx) {
            console.error('[Simli-VideoWS] cannot set up frame push — elements missing');
            return;
          }
          // @ts-ignore - Browser WebSocket, not Node.js ws module
          const ws = new WebSocket(`ws://127.0.0.1:${port}`);
          ws.binaryType = 'arraybuffer';
          let pushing = false;
          let inFlight = false; // skip a tick if the previous encode/send hasn't finished
          ws.onopen = () => {
            console.log('[Simli-VideoWS] Connected to video relay');
            // @ts-ignore
            window.__simliVideoWs = ws;
            pushing = true;
            const intervalMs = Math.round(1000 / fps);
            // Downscale to maxDim (longest side) before JPEG-encoding: the
            // dominant per-frame cost scales with pixel count, so a 512→384
            // shrink is ~0.56× the work, and lower fps multiplies the saving.
            setInterval(() => {
              if (!pushing || ws.readyState !== 1) return;
              if (video.readyState < 2) return;
              if (inFlight) return; // don't queue encodes faster than they drain
              const vw = video.videoWidth || 512;
              const vh = video.videoHeight || 512;
              const scale = Math.min(1, maxDim / Math.max(vw, vh));
              canvas.width = Math.max(1, Math.round(vw * scale));
              canvas.height = Math.max(1, Math.round(vh * scale));
              ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
              inFlight = true;
              canvas.toBlob(
                (blob) => {
                  if (!blob || ws.readyState !== 1) { inFlight = false; return; }
                  blob.arrayBuffer().then((buf) => {
                    try { ws.send(buf); } catch (e) { /* ignore */ }
                    inFlight = false;
                  }).catch(() => { inFlight = false; });
                },
                'image/jpeg',
                quality,
              );
            }, intervalMs);
          };
          ws.onclose = () => {
            pushing = false;
            // @ts-ignore
            window.__simliVideoWs = null;
            console.log('[Simli-VideoWS] Video relay disconnected');
          };
          // @ts-ignore
          ws.onerror = () => console.error('[Simli-VideoWS] Video relay error');
          },
          { port: this.videoWsPort, fps: this.videoFps, maxDim: this.videoMaxDim, quality: this.videoJpegQuality },
        );
        console.log('✓ Simli page connected to WebSocket video relay');
      }

      this._isConnected = true;
      this._isReady = true;
      console.log('✓ Simli Avatar initialized successfully (audio isolated, WebSocket audio + video relay active)');

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

      await meetingPage.addInitScript(({ videoWsPort, captureFps }: { videoWsPort: number; captureFps: number }) => {
        // NOTE: this runs in EVERY frame (main doc + Zoom's about:blank /
        // reCAPTCHA iframes). Zoom captures the camera from a subframe, so the
        // override must be installed everywhere — but the relay connection and
        // canvas are set up LAZILY on the first getUserMedia({video}) call, so
        // the throwaway frames that never request video never connect (avoids
        // the connect/disconnect storm).
        console.log(`[LMA-Simli] init script running in frame: ${location.href} (top=${window.top === window.self})`);
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          console.log('[LMA-Simli] navigator.mediaDevices.getUserMedia NOT available in this frame — override skipped');
          return;
        }
        const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);

        let avatarCanvas: HTMLCanvasElement | null = null;
        let avatarCtx: CanvasRenderingContext2D | null = null;
        let framesDrawn = 0;
        let relayStarted = false;
        // @ts-ignore
        window.__simliFramesDrawn = 0;

        const drawFrame = (buf: ArrayBuffer) => {
          if (!avatarCanvas || !avatarCtx) return;
          const blob = new Blob([buf], { type: 'image/jpeg' });
          createImageBitmap(blob)
            .then((bmp) => {
              if (bmp.width && (avatarCanvas!.width !== bmp.width || avatarCanvas!.height !== bmp.height)) {
                avatarCanvas!.width = bmp.width;
                avatarCanvas!.height = bmp.height;
              }
              avatarCtx!.drawImage(bmp, 0, 0, avatarCanvas!.width, avatarCanvas!.height);
              bmp.close();
              framesDrawn++;
              // @ts-ignore
              window.__simliFramesDrawn = framesDrawn;
              if (framesDrawn % 200 === 0) {
                console.log(`[LMA-Simli] avatar frames drawn: ${framesDrawn}`);
              }
            })
            .catch(() => { /* drop bad frame */ });
        };

        const connectVideoWs = () => {
          // @ts-ignore
          const ws = new WebSocket(`ws://127.0.0.1:${videoWsPort}`);
          ws.binaryType = 'arraybuffer';
          ws.onopen = () => console.log('[LMA-Simli] Video relay connected');
          // @ts-ignore
          ws.onmessage = (event: any) => drawFrame(event.data as ArrayBuffer);
          ws.onclose = () => {
            console.log('[LMA-Simli] Video relay closed — retrying in 1s');
            setTimeout(connectVideoWs, 1000);
          };
          // @ts-ignore
          ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
          // @ts-ignore
          window.__simliVideoWs = ws;
        };

        // Set up the canvas + relay once, on demand. The canvas itself is
        // long-lived — frames are continuously drawn onto it from the relay.
        const startRelay = () => {
          if (relayStarted) return;
          relayStarted = true;
          avatarCanvas = document.createElement('canvas');
          avatarCanvas.width = 512;
          avatarCanvas.height = 512;
          avatarCtx = avatarCanvas.getContext('2d');
          connectVideoWs();
        };

        // Eagerly connect this frame as a relay consumer so frames are
        // already flowing onto the canvas before Zoom asks for the camera.
        // Node calls this (via the __simliEnsureConsumer binding) when the
        // meeting URL loads, instead of waiting for Zoom to spontaneously
        // call getUserMedia — which it may never do if the prejoin shows
        // video as already "on". Safe to call repeatedly (startRelay guards).
        // @ts-ignore
        window.__simliEnsureConsumer = () => {
          try {
            startRelay();
            console.log('[LMA-Simli] consumer pre-warmed via __simliEnsureConsumer');
            return true;
          } catch (e) {
            console.log('[LMA-Simli] __simliEnsureConsumer failed: ' + (e as Error).message);
            return false;
          }
        };

        const hasFrames = () => framesDrawn > 0;

        // Mint a FRESH capture stream off the persistent canvas on every
        // getUserMedia({video}) call rather than reusing one shared track.
        // Zoom calls track.stop() on the preview video track when it
        // transitions preview → meeting; if we hand out a single shared
        // track, that stop() ends our only source, and every later
        // getUserMedia returns an ended track. Zoom's virtual-background
        // pipeline then throws "MediaStreamTrackProcessor: Input track cannot
        // be ended" (USER_FORBIDDED_CAPTURE_VIDEO) and turns the camera off.
        // A fresh captureStream off the same canvas is independent — stopping
        // one track never affects the canvas or any other handed-out track.
        const mintVideoTrack = (): MediaStreamTrack | undefined => {
          // captureStream at captureFps off the avatar canvas. This sets the
          // rate Zoom's WebRTC encoder runs at — the single biggest video CPU
          // cost — so keep it at/below the producer fps (no point capturing
          // faster than the canvas updates).
          const freshStream = avatarCanvas!.captureStream(captureFps);
          // @ts-ignore
          window.__simliCanvasStream = freshStream;
          return freshStream.getVideoTracks()[0];
        };

        const buildSimliStream = async (
          constraints: MediaStreamConstraints,
        ): Promise<MediaStream> => {
          const videoTrack = mintVideoTrack();
          console.log(
            `[LMA-Simli] Returning Simli avatar stream — framesDrawn=${framesDrawn}, ` +
              `track readyState=${videoTrack?.readyState}`,
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
            // Lazily set up the relay + canvas in whichever frame actually
            // requests the camera (Zoom uses a subframe).
            startRelay();
            // Wait for the first relayed frame so we never hand Zoom an empty
            // canvas (which it renders as a black tile / camera-off).
            const startTs = Date.now();
            const budgetMs = 8000;
            while (!hasFrames() && Date.now() - startTs < budgetMs) {
              await new Promise((r) => setTimeout(r, 50));
            }
            if (hasFrames()) {
              console.log(`[LMA-Simli] ✓ First frame after ${Date.now() - startTs}ms`);
              return buildSimliStream(constraints);
            }
            console.log('[LMA-Simli] ⚠️ No avatar frames after 8s — falling through');
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
      }, { videoWsPort: this.videoWsPort, captureFps: this.captureFps });

      console.log('✓ getUserMedia override injected into meeting page');
    } catch (error) {
      console.error('Failed to inject getUserMedia override:', error);
    }
  }

  /**
   * Eagerly connect the meeting page (and its subframes) to the video relay
   * so avatar frames are already flowing onto the in-page canvas before Zoom
   * requests the camera. Without this the consumer connection is purely
   * lazy — it only happens if/when the platform calls getUserMedia({video}),
   * which Zoom may never do when its prejoin shows video as already "on",
   * leaving the relay with 0 consumers and the camera blank.
   *
   * Calls the __simliEnsureConsumer binding installed by the getUserMedia
   * override. Idempotent (the override guards re-entry), so it's safe to
   * call on every meeting-URL navigation and across all frames.
   */
  async connectStreamToMeetingPage(meetingPage: Page): Promise<void> {
    if (!this.enabled || !this._isReady || !this.simliPage) return;
    try {
      const frames = meetingPage.frames();
      let triggered = 0;
      for (const frame of frames) {
        try {
          const ok = await frame.evaluate(() => {
            // @ts-ignore — binding installed by the getUserMedia override
            if (typeof window.__simliEnsureConsumer === 'function') {
              // @ts-ignore
              return !!window.__simliEnsureConsumer();
            }
            return false;
          });
          if (ok) triggered += 1;
        } catch {
          // Frame may be cross-origin / mid-navigation / detached — skip.
        }
      }
      console.log(
        `Simli avatar frame relay: pre-warmed consumer in ${triggered}/${frames.length} frame(s).`,
      );
    } catch (error) {
      console.error('Failed to connect Simli frame relay to meeting page:', error);
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
   * Video frame relay server. The Simli page connects and streams JPEG frames;
   * we keep only the most recent one in memory for the meeting page to pull.
   */
  private async startVideoFrameRelay(): Promise<void> {
    return new Promise((resolve) => {
      this.videoWsServer = new WebSocketServer({ port: 0, host: '127.0.0.1' });

      this.videoWsServer.on('listening', () => {
        const addr = this.videoWsServer.address();
        this.videoWsPort = typeof addr === 'object' ? addr.port : 0;
        console.log(`✓ Simli video relay listening on ws://127.0.0.1:${this.videoWsPort}`);
        resolve();
      });

      // Both the Simli page (producer) and the meeting page (consumer) connect
      // here. Forward every frame from the producer to all other clients, and
      // replay the latest frame to a newly-connected consumer so it draws
      // immediately rather than waiting for the next push.
      this.videoWsServer.on('connection', (ws: WebSocket) => {
        const clientCount = this.videoWsServer.clients.size;
        // First client is the Simli page (producer); any later client is a
        // meeting-page consumer. Surfacing this makes "0 consumer(s)" (avatar
        // never reaches the meeting) vs "1+ consumer(s)" obvious in the logs.
        const consumerCount = Math.max(0, clientCount - 1);
        console.log(
          `✓ Client connected to Simli video relay (total ${clientCount}, ${consumerCount} consumer(s))`,
        );
        if (this.latestFrame) {
          try { ws.send(this.latestFrame); } catch (e) { /* ignore */ }
        }
        let framesFromThisClient = 0;
        let framesFwdThisClient = 0;
        ws.on('message', (data: Buffer) => {
          this.latestFrame = data;
          framesFromThisClient++;
          for (const client of this.videoWsServer.clients) {
            if (client !== ws && client.readyState === WebSocket.OPEN) {
              try { client.send(data); framesFwdThisClient++; } catch (e) { /* ignore */ }
            }
          }
          if (framesFromThisClient % 100 === 0) {
            console.log(`[Simli-DEBUG relay] producer sent ${framesFromThisClient} frames; forwarded ${framesFwdThisClient} to consumers (${this.videoWsServer.clients.size - 1} consumer(s))`);
          }
        });
        ws.on('close', () => console.log(`🎥 Simli video relay client disconnected (received ${framesFromThisClient} frames from it)`));
        ws.on('error', (err: Error) => console.error('❌ Simli video relay error:', err.message));
      });

      this.videoWsServer.on('error', (err: Error) => {
        console.error('❌ Simli video relay server error:', err);
        resolve();
      });
    });
  }

  private stopVideoFrameRelay(): void {
    this.latestFrame = null;
    if (this.videoWsServer) {
      try { this.videoWsServer.close(); } catch (e) { /* ignore */ }
      this.videoWsServer = null;
      console.log('✓ Simli video relay stopped');
    }
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
    
    // Stop WebSocket audio bridge + video relay first
    this.stopAudioWebSocket();
    this.stopVideoFrameRelay();
    
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
