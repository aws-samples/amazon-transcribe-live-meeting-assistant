/**
 * video-start-time.ts — when the screen recording's first frame was captured.
 *
 * A moment's position in the recording is its wall-clock time minus the
 * wall-clock time of the recording's first frame (key frames store it as
 * VideoOffset). FFmpeg can take tens of seconds between being launched and
 * grabbing its first frame (on a MicroVM it took 26 s), and nothing from that
 * gap is in the recording, so the launch time is not the start of the
 * recording's timeline.
 *
 * FFmpeg reports the first frame's time itself. x11grab stamps each frame with
 * the wall clock in microseconds, and FFmpeg prints the first one when it
 * describes the input:
 *
 *   Input #0, x11grab, from ':99':
 *     Duration: N/A, start: 1794409351.282578, bitrate: 331776 kb/s
 *
 * The output timeline starts at that frame (FFmpeg subtracts each input's start
 * time), so that is the recording's start. If FFmpeg's output does not carry
 * it (another FFmpeg build, another log format), the time FFmpeg opens the
 * first segment is used instead: it is within a second or two of the first
 * frame, which is still far closer than the launch time.
 *
 * Plain logic with no imports, so it is unit-tested directly.
 */

/**
 * The x11grab input's start time, in seconds since the epoch. The number must
 * be followed by its separator so a chunk that ends mid-number is not read.
 */
const X11GRAB_START = /Input #\d+, x11grab\b[^\n]*\n\s*Duration:[^\n]*?\bstart:\s*(\d+(?:\.\d+)?)(?=[,\s])/;
/** FFmpeg's segment muxer opening the first segment. */
const FIRST_SEGMENT_OPENED = /Opening '[^']*segment_0+\.ts' for writing/;
/**
 * How far a reported start may lie outside [launch, now] and still be taken.
 * Anything further off is not a wall-clock reading (a build that stamps frames
 * with another clock) and is ignored in favour of the first-segment time.
 */
const PLAUSIBLE_SLACK_MS = 5_000;
/** Keep only this much unparsed stderr; the input description is near the top. */
const MAX_BUFFER_CHARS = 64 * 1024;

export type RecordingStartSource = 'first-frame' | 'first-segment';

/**
 * Reads FFmpeg's stderr, chunk by chunk as it arrives, until it knows when the
 * recording's first frame was captured.
 */
export class RecordingStartClock {
  private buffer = '';
  private _startMs: number | null = null;
  private _source: RecordingStartSource | null = null;

  /** @param launchedAtMs wall-clock time FFmpeg was launched */
  constructor(private readonly launchedAtMs: number) {}

  /** Wall-clock time of the first captured frame, or null while not yet known. */
  get startMs(): number | null {
    return this._startMs;
  }

  /** Where startMs came from, or null while not yet known. */
  get source(): RecordingStartSource | null {
    return this._source;
  }

  /**
   * Feed one chunk of FFmpeg's stderr. Returns true when this chunk settled the
   * start time (so the caller can log it once).
   *
   * @param nowMs wall-clock time the chunk arrived
   */
  feed(chunk: string, nowMs: number): boolean {
    if (this._startMs !== null) return false;
    this.buffer += chunk;
    if (this.buffer.length > MAX_BUFFER_CHARS) {
      this.buffer = this.buffer.slice(-MAX_BUFFER_CHARS);
    }

    const m = X11GRAB_START.exec(this.buffer);
    if (m) {
      const ms = Math.round(parseFloat(m[1]) * 1000);
      if (
        Number.isFinite(ms) &&
        ms >= this.launchedAtMs - PLAUSIBLE_SLACK_MS &&
        ms <= nowMs + PLAUSIBLE_SLACK_MS
      ) {
        return this.settle(ms, 'first-frame');
      }
    }
    // The input is always described before the first segment is opened, so by
    // the time the segment is opened the first-frame time is known or absent.
    if (FIRST_SEGMENT_OPENED.test(this.buffer)) {
      return this.settle(nowMs, 'first-segment');
    }
    return false;
  }

  private settle(ms: number, source: RecordingStartSource): boolean {
    this._startMs = ms;
    this._source = source;
    this.buffer = '';
    return true;
  }
}
