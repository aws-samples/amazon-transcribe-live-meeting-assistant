/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The wake-phrase activation window as an idle timeout rather than a deadline
 * from the wake phrase, for a voice-assistant provider to drive from its own
 * busy signals. Both the Nova Sonic and the ElevenLabs agents use it.
 *
 * `VoiceAssistantActivationDuration` is the quiet time after the assistant was
 * last busy or engaged. The window does not lapse while a lookup is running,
 * while the assistant's speech is still arriving or still to be played, or
 * (briefly) while the output detector still hears it; being addressed again or
 * spoken over restarts the countdown. A participant's turn only nudges a
 * lapsing window, and never beyond twice its length, so talk among the other
 * participants cannot keep a session open indefinitely.
 *
 * Why, as recorded on a live meeting with the Nova Sonic agent and a 30-second
 * window that was then a deadline from the wake phrase: a wake phrase at
 * 22:08:49, two lookups, and an answer that started at 22:09:11 and was audible
 * for 26 seconds, which the session closed 8 seconds into — leaving the
 * participant who then said "stop" talking to a session that could no longer
 * hear it. A session closed while the meeting is still hearing the assistant
 * cannot hear the participant who asks it to stop, and a session closed while
 * a lookup is running throws its answer away (#831).
 */

/** How often a held-open window looks again at whether the assistant has gone idle. */
export const ACTIVATION_RECHECK_MS = 1000;

/** How far past the end of the assistant's own playback the output detector
 *  may still hold the window open. A detector that never reports silence must
 *  not keep a wake-phrase session open for the rest of the meeting. */
export const ACTIVATION_AUDIBLE_HOLD_MAX_MS = 10000;

/**
 * How long a participant's turn holds a lapsing window open, so that a
 * question asked just before it lapses is not cut off before the model has had
 * the moment it needs to start answering or call a tool.
 *
 * Deliberately short and deliberately not a re-arm: the session hears the
 * whole meeting, so treating every turn as engagement would keep a wake-phrase
 * session open for as long as anyone in the meeting is talking. Being addressed
 * by wake phrase is what re-arms the full window.
 */
export const ACTIVATION_USER_TURN_GRACE_MS = 3000;

export type ActivationHoldReason = 'tool' | 'audio-arriving' | 'playback' | 'audible' | 'user-turn';

/** What the provider reports about itself. Every member is read at check time. */
export interface ActivationWindowSignals {
  /** A lookup (client tool call) is in progress. */
  toolRunning(): boolean;
  /** Assistant speech is arriving from the model or queued/playing locally. */
  audioArriving(): boolean;
  /** When the assistant's playback ends, or last ended (ms since the epoch;
   *  0 if it has never played). */
  playbackEndsAt(): number;
  /** The output detector hears the assistant in the meeting right now. */
  audible(): boolean;
  /** When a participant's turn was last heard (ms since the epoch; 0 if never). */
  lastUserTurnAt(): number;
}

export class ActivationWindow {
  private timer: NodeJS.Timeout | null = null;
  private windowMs = 0;
  private running = false;
  /** When the idle countdown last restarted. */
  private lastEngagementAt = 0;
  /** What the window is being held open for, so it is logged once per change. */
  private holdReason: ActivationHoldReason | null = null;

  constructor(
    private readonly signals: ActivationWindowSignals,
    /** Called once when the window lapses; the provider deactivates. */
    private readonly onLapse: () => void,
  ) {}

  /** Open (or reopen) the window with an idle duration of `windowMs`. */
  start(windowMs: number): void {
    this.windowMs = windowMs;
    this.running = true;
    this.holdReason = null;
    this.noteEngagement();
    this.schedule(windowMs);
  }

  /** Stop the window without lapsing it (deactivation, shutdown). */
  stop(): void {
    this.running = false;
    this.holdReason = null;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  isRunning(): boolean {
    return this.running;
  }

  /** Restart the idle countdown: a lookup finished, a barge-in, being addressed. */
  noteEngagement(): void {
    this.lastEngagementAt = Date.now();
  }

  /** Restart the countdown of a running window and say why. */
  rearm(why: string): void {
    if (!this.running) return;
    console.log(`🔁 Activation window re-armed (${why})`);
    this.noteEngagement();
  }

  /** Why the window must not lapse yet, or null if nothing holds it. */
  holdFor(now: number): ActivationHoldReason | null {
    const s = this.signals;
    if (s.toolRunning()) return 'tool';
    if (s.audioArriving()) return 'audio-arriving';
    const playbackEndsAt = s.playbackEndsAt();
    if (playbackEndsAt > now) return 'playback';
    if (s.audible() && now - playbackEndsAt < ACTIVATION_AUDIBLE_HOLD_MAX_MS) return 'audible';
    const idleSince = Math.max(this.lastEngagementAt, playbackEndsAt);
    if (now - s.lastUserTurnAt() < ACTIVATION_USER_TURN_GRACE_MS && now - idleSince < 2 * this.windowMs) {
      return 'user-turn';
    }
    return null;
  }

  private schedule(delayMs: number): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.check();
    }, Math.max(0, delayMs));
  }

  private check(): void {
    if (!this.running) return;
    const now = Date.now();
    const hold = this.holdFor(now);
    if (hold) {
      if (hold !== this.holdReason) {
        console.log(`⏳ Activation window held open (${hold})`);
      }
      this.holdReason = hold;
      // A participant's turn is not engagement; everything else is the
      // assistant being busy, and the idle countdown starts again when it stops.
      if (hold !== 'user-turn') this.noteEngagement();
      this.schedule(ACTIVATION_RECHECK_MS);
      return;
    }
    this.holdReason = null;
    const idleSince = Math.max(this.lastEngagementAt, this.signals.playbackEndsAt());
    const remaining = this.windowMs - (now - idleSince);
    if (remaining > 0) {
      this.schedule(remaining);
      return;
    }
    console.log(`⏰ Activation window elapsed (idle ${now - idleSince} ms of ${this.windowMs} ms)`);
    this.running = false;
    this.onLapse();
  }
}
