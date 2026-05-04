/**
 * AgentSpeakingDetector
 *
 * Platform-agnostic "is the voice assistant currently speaking?" detector.
 * Listens to the PCM samples produced by the voice agent via PulseAudio and
 * emits "started" / "stopped" events based on RMS magnitude with hysteresis.
 *
 * Audio routing (see backend/entrypoint.sh):
 *
 *   Agent PCM → pacat → agent_output (null sink)
 *     ├─ module-loopback → combined_audio sink → Transcribe
 *     └─ module-remap-source (agent_mic) → Chromium microphone
 *
 *   agent_output.monitor is the authoritative "agent audio" source.
 *
 * Because human audio arrives via WebRTC (not agent_mic), this signal cleanly
 * distinguishes agent speech from human speech regardless of meeting platform
 * or voice-assistant provider.
 */

import { spawn, ChildProcess } from 'child_process';
import { EventEmitter } from 'events';

export interface AgentSpeakingDetectorOptions {
  /** PulseAudio source device to listen to. Default: agent_output.monitor */
  sourceName?: string;
  /** Sample rate in Hz. Must match `pacat --rate`. Default: 16000 */
  sampleRate?: number;
  /** RMS threshold (int16 scale, ~0-32767) above which we consider speech. */
  onRms?: number;
  /**
   * Milliseconds of continuous sub-threshold audio before emitting 'stopped'.
   * Long enough to absorb natural comma/intonation pauses inside a single
   * agent utterance so short trailing tokens do not get mis-attributed.
   * Default 2000.
   */
  offMs?: number;
  /** RMS computation window size in milliseconds. */
  windowMs?: number;
  /** Periodic RMS log cadence (0 to disable). Default: 0 (off). */
  logEveryMs?: number;
}

export class AgentSpeakingDetector extends EventEmitter {
  private proc: ChildProcess | null = null;
  private _isSpeaking = false;
  private lastHighTs = 0;
  private lastLogTs = 0;
  private recentRms = 0; // most recent RMS value (for optional periodic log)

  private readonly sourceName: string;
  private readonly sampleRate: number;
  private readonly onRms: number;
  private readonly offMs: number;
  private readonly windowMs: number;
  private readonly logEveryMs: number;

  constructor(opts: AgentSpeakingDetectorOptions = {}) {
    super();
    this.sourceName = opts.sourceName ?? 'agent_output.monitor';
    this.sampleRate = opts.sampleRate ?? 16000;
    this.onRms = opts.onRms ?? 500;
    this.offMs = opts.offMs ?? 2000;
    this.windowMs = opts.windowMs ?? 50;
    this.logEveryMs = opts.logEveryMs ?? 0;
  }

  /** Begin recording from PulseAudio and monitoring magnitude. */
  start(): void {
    if (this.proc) {
      console.log('🎙️  AgentSpeakingDetector already running');
      return;
    }

    console.log(
      `🎙️  AgentSpeakingDetector starting: device=${this.sourceName}, ` +
        `rate=${this.sampleRate}, onRms=${this.onRms}, offMs=${this.offMs}`,
    );

    this.proc = spawn('pacat', [
      '--record',
      `--device=${this.sourceName}`,
      '--format=s16le',
      `--rate=${this.sampleRate}`,
      '--channels=1',
      '--latency-msec=50',
    ]);

    const samplesPerWindow = Math.floor((this.sampleRate * this.windowMs) / 1000);
    const bytesPerWindow = samplesPerWindow * 2; // 16-bit = 2 bytes/sample
    let buf = Buffer.alloc(0);

    this.proc.stdout?.on('data', (chunk: Buffer) => {
      buf = Buffer.concat([buf, chunk]);
      while (buf.length >= bytesPerWindow) {
        const window = buf.subarray(0, bytesPerWindow);
        buf = buf.subarray(bytesPerWindow);
        this.processWindow(window, samplesPerWindow);
      }
    });

    this.proc.stderr?.on('data', (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) console.log(`🎙️  agent-detector pacat: ${msg}`);
    });

    this.proc.on('close', (code) => {
      console.log(`🎙️  AgentSpeakingDetector pacat exited (code=${code})`);
      this.proc = null;
      if (this._isSpeaking) {
        this._isSpeaking = false;
        this.emit('stopped');
      }
    });

    this.proc.on('error', (err) => {
      console.error('🎙️  AgentSpeakingDetector pacat error:', err);
    });
  }

  /** Stop the detector and clean up. */
  stop(): void {
    if (!this.proc) return;
    try {
      this.proc.kill('SIGTERM');
    } catch {
      /* ignore */
    }
    this.proc = null;
    if (this._isSpeaking) {
      this._isSpeaking = false;
      this.emit('stopped');
    }
    console.log('🎙️  AgentSpeakingDetector stopped');
  }

  isSpeaking(): boolean {
    return this._isSpeaking;
  }

  /** Current most-recent RMS value (for diagnostics / threshold tuning). */
  currentRms(): number {
    return this.recentRms;
  }

  private processWindow(window: Buffer, samples: number): void {
    // Compute RMS of int16 samples in the window.
    let sumSq = 0;
    for (let i = 0; i < window.length; i += 2) {
      const s = window.readInt16LE(i);
      sumSq += s * s;
    }
    const rms = Math.sqrt(sumSq / samples);
    this.recentRms = rms;

    const now = Date.now();

    // Optional periodic RMS log for tuning (disabled by default).
    if (this.logEveryMs > 0 && now - this.lastLogTs >= this.logEveryMs) {
      this.lastLogTs = now;
      console.log(
        `🎙️  RMS=${rms.toFixed(0)} speaking=${this._isSpeaking} onRms=${this.onRms}`,
      );
    }

    if (rms >= this.onRms) {
      this.lastHighTs = now;
      if (!this._isSpeaking) {
        this._isSpeaking = true;
        console.log(
          `🎙️  Agent speaking ON  (RMS=${rms.toFixed(0)}, threshold=${this.onRms})`,
        );
        this.emit('started');
      }
    } else if (this._isSpeaking && now - this.lastHighTs >= this.offMs) {
      this._isSpeaking = false;
      console.log(
        `🔇 Agent speaking OFF (RMS=${rms.toFixed(0)}, silence=${now - this.lastHighTs}ms)`,
      );
      this.emit('stopped');
    }
  }
}

/**
 * Singleton detector instance. Start once from index.ts when
 * voiceAssistant.isEnabled() is true. Thresholds are tunable via env vars.
 */
export const agentSpeakingDetector = new AgentSpeakingDetector({
  onRms: parseInt(process.env.AGENT_DETECTOR_ON_RMS || '500', 10),
  offMs: parseInt(process.env.AGENT_DETECTOR_OFF_MS || '2000', 10),
  logEveryMs: parseInt(process.env.AGENT_DETECTOR_LOG_MS || '0', 10),
});


