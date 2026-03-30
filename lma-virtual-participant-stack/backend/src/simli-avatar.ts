/**
 * Simli Avatar Manager
 * 
 * Manages a Simli avatar session that provides lip-synced video for the
 * Virtual Participant's camera feed. The avatar is driven by audio from
 * the voice assistant (Nova Sonic or ElevenLabs).
 * 
 * Architecture:
 * - A background Puppeteer page loads the Simli JS SDK via CDN
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

import { Browser, Page } from 'puppeteer';

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

  async initialize(browser: Browser): Promise<void> {
    if (!this.enabled) {
      console.log('Simli Avatar disabled - skipping initialization');
      return;
    }

    try {
      console.log('Initializing Simli Avatar...');

      this.simliPage = await browser.newPage();
      const simliPageHtml = this.buildSimliPageHtml();
      
      // AUDIO ISOLATION: Patch AudioNode.connect() BEFORE any scripts load.
      // This blocks audio from reaching the speakers (AudioDestinationNode)
      // while keeping the WebRTC connection alive for video rendering.
      // Tested and confirmed working in standalone browser test.
      await this.simliPage.evaluateOnNewDocument(() => {
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
      
      await this.simliPage.setContent(simliPageHtml, { waitUntil: 'networkidle0' });
      console.log('✓ Simli page loaded with audio isolation patch');
      
      console.log('Waiting for Simli avatar to connect...');
      
      const initResult = await this.simliPage.evaluate(async (config: { apiKey: string; faceId: string; maxSessionLength: number; maxIdleTime: number; transportMode: string }) => {
        try {
          // @ts-ignore
          const { SimliClient, generateSimliSessionToken } = window.SimliModule;
          
          const sessionToken = await generateSimliSessionToken({
            apiKey: config.apiKey,
            config: {
              faceId: config.faceId,
              handleSilence: true,
              maxSessionLength: config.maxSessionLength,
              maxIdleTime: config.maxIdleTime,
            },
          });

          const videoElement = document.getElementById('simli-video') as HTMLVideoElement;
          // Use in-DOM muted audio element (tested and confirmed to block audio)
          const audioElement = document.getElementById('simli-audio') as HTMLAudioElement;

          if (!videoElement || !audioElement) {
            throw new Error('Video or audio elements not found');
          }

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

          simliClient.on('start', () => {
            console.log('[Simli] Avatar connected and visible');
            // @ts-ignore
            window.__simliReady = true;
          });
          simliClient.on('stop', () => {
            console.log('[Simli] Connection stopped');
            // @ts-ignore
            window.__simliReady = false;
          });
          simliClient.on('error', (err: any) => console.error('[Simli] Error:', err));
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

      this._isConnected = true;
      this._isReady = true;
      console.log('✓ Simli Avatar initialized successfully (audio isolated)');

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

      const client = await meetingPage.createCDPSession();
      const simliClient = await this.simliPage.createCDPSession();
      
      await this.simliPage.evaluate(() => {
        const video = document.getElementById('simli-video') as HTMLVideoElement;
        const canvas = document.getElementById('simli-canvas') as HTMLCanvasElement;
        const ctx = canvas.getContext('2d');
        
        if (!video || !canvas || !ctx) {
          console.error('[Simli] Cannot set up canvas capture - elements missing');
          return;
        }

        const updateCanvasSize = () => {
          canvas.width = video.videoWidth || 640;
          canvas.height = video.videoHeight || 480;
        };
        video.addEventListener('loadedmetadata', updateCanvasSize);
        updateCanvasSize();

        const drawFrame = () => {
          if (video.readyState >= 2) {
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          }
          requestAnimationFrame(drawFrame);
        };
        drawFrame();

        const canvasStream = canvas.captureStream(30);
        // @ts-ignore
        window.__simliCanvasStream = canvasStream;
        console.log('[Simli] Canvas capture stream created');
      });

      await meetingPage.evaluateOnNewDocument(() => {
        const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        let simliVideoTrack: MediaStreamTrack | null = null;
        let simliStreamReady = false;

        navigator.mediaDevices.getUserMedia = async function(constraints?: MediaStreamConstraints): Promise<MediaStream> {
          console.log('[LMA-Simli] getUserMedia called with constraints:', JSON.stringify(constraints));
          
          if (constraints?.video && simliStreamReady && simliVideoTrack) {
            console.log('[LMA-Simli] Returning Simli avatar video stream');
            if (constraints.audio) {
              const audioStream = await originalGetUserMedia({ audio: constraints.audio });
              const combinedStream = new MediaStream();
              combinedStream.addTrack(simliVideoTrack);
              audioStream.getAudioTracks().forEach(track => combinedStream.addTrack(track));
              return combinedStream;
            } else {
              return new MediaStream([simliVideoTrack]);
            }
          }
          
          console.log('[LMA-Simli] Falling through to original getUserMedia');
          return originalGetUserMedia(constraints);
        };

        // @ts-ignore
        window.__setSimliVideoTrack = (track: MediaStreamTrack) => {
          simliVideoTrack = track;
          simliStreamReady = true;
          console.log('[LMA-Simli] Simli video track set - getUserMedia override active');
        };
        // @ts-ignore
        window.__simliOverrideInstalled = true;
        console.log('[LMA-Simli] getUserMedia override installed');
      });

      console.log('✓ getUserMedia override injected into meeting page');
    } catch (error) {
      console.error('Failed to inject getUserMedia override:', error);
    }
  }

  async connectStreamToMeetingPage(meetingPage: Page): Promise<void> {
    if (!this.enabled || !this._isReady || !this.simliPage) return;

    try {
      console.log('Connecting Simli video stream to meeting page...');

      const offer = await this.simliPage.evaluate(async () => {
        // @ts-ignore
        const stream = window.__simliCanvasStream as MediaStream;
        if (!stream) throw new Error('No canvas stream available');

        const pc = new RTCPeerConnection();
        // @ts-ignore
        window.__simliPC = pc;
        const videoTrack = stream.getVideoTracks()[0];
        if (videoTrack) pc.addTrack(videoTrack, stream);

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await new Promise<void>((resolve) => {
          if (pc.iceGatheringState === 'complete') resolve();
          else pc.addEventListener('icegatheringstatechange', () => {
            if (pc.iceGatheringState === 'complete') resolve();
          });
        });
        return JSON.stringify(pc.localDescription);
      });

      const answer = await meetingPage.evaluate(async (offerStr: string) => {
        const offer = JSON.parse(offerStr);
        const pc = new RTCPeerConnection();
        // @ts-ignore
        window.__simliReceiverPC = pc;
        pc.ontrack = (event) => {
          console.log('[LMA-Simli] Received video track from Simli page');
          // @ts-ignore
          if (window.__setSimliVideoTrack) window.__setSimliVideoTrack(event.track);
        };
        await pc.setRemoteDescription(offer);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        await new Promise<void>((resolve) => {
          if (pc.iceGatheringState === 'complete') resolve();
          else pc.addEventListener('icegatheringstatechange', () => {
            if (pc.iceGatheringState === 'complete') resolve();
          });
        });
        return JSON.stringify(pc.localDescription);
      }, offer);

      await this.simliPage.evaluate(async (answerStr: string) => {
        const answer = JSON.parse(answerStr);
        // @ts-ignore
        const pc = window.__simliPC as RTCPeerConnection;
        await pc.setRemoteDescription(answer);
        console.log('[Simli] Peer connection established with meeting page');
      }, answer);

      let connected = false;
      for (let i = 0; i < 10; i++) {
        connected = await meetingPage.evaluate(() => {
          // @ts-ignore
          return window.__simliOverrideInstalled === true;
        });
        if (connected) break;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      console.log(connected ? '✓ Simli video stream connected to meeting page' : '⚠️  Simli stream connection could not be verified');
    } catch (error) {
      console.error('Failed to connect Simli stream to meeting page:', error);
    }
  }

  async sendAudioData(audioData: Buffer): Promise<void> {
    if (!this.enabled || !this._isConnected || !this.simliPage) return;

    try {
      this.audioChunkCount++;
      if (this.audioChunkCount % 50 === 0) {
        console.log(`🎭 Sent ${this.audioChunkCount} audio chunks to Simli avatar (${audioData.length} bytes each)`);
      }

      const audioBase64 = audioData.toString('base64');
      await this.simliPage.evaluate((base64Data: string) => {
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
      }, audioBase64);
    } catch (error) {
      if (this.audioChunkCount % 100 === 0) {
        console.error('Error sending audio to Simli:', error);
      }
    }
  }

  async stop(): Promise<void> {
    if (!this.enabled) return;
    console.log('Stopping Simli Avatar...');
    try {
      if (this.simliPage) {
        await this.simliPage.evaluate(() => {
          try {
            // @ts-ignore
            if (window.__simliClient) window.__simliClient.stop();
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
        const module = await import('https://esm.sh/simli-client');
        window.SimliModule = module;
        document.getElementById('status').textContent = 'Simli Avatar: SDK Loaded';
        console.log('[Simli] SDK loaded successfully');
      } catch (error) {
        console.error('[Simli] Failed to load SDK from CDN:', error);
        try {
          const module = await import('https://unpkg.com/simli-client/dist/index.mjs');
          window.SimliModule = module;
          document.getElementById('status').textContent = 'Simli Avatar: SDK Loaded (fallback)';
        } catch (error2) {
          console.error('[Simli] Failed to load SDK from fallback CDN:', error2);
          document.getElementById('status').textContent = 'Simli Avatar: SDK Load Failed';
        }
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
