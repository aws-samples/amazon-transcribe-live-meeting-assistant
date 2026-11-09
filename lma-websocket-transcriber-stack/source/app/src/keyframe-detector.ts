/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Decides which samples of the meeting display are key frames: the moments
 * where the shared content (a slide, a document page) has changed and then
 * settled.
 *
 * A copy of the Virtual Participant's keyframe-detector.ts
 * (lma-virtual-participant-stack/backend/src), so the desktop capture app's
 * video is judged by the same rule. That one is the original;
 * videorecording-keyframes.test.ts fails when the two differ in anything but
 * comments and braces. The sampler
 * (videokeyframes.ts) feeds it a small grayscale image of each key frame of
 * the incoming H.264 stream, one about every two seconds (a 64x36 grid, each
 * cell the average of a block of the picture). Two comparisons are made:
 *
 *   - against the previous sample and against the first sample of the
 *     current still run, to tell whether the picture is still moving. Content
 *     that is moving (a slide transition, scrolling, a video or a talking
 *     head) is not captured until it holds still for `settleSamples`
 *     consecutive samples, so a frame is never taken mid-transition. The
 *     second comparison stops a screen that drifts a little every second (a
 *     live web page) from counting as one long still run, whose frames would
 *     all be stamped with the run's start.
 *   - against the last frame that was kept, to tell whether the settled
 *     picture is new. Returning to a slide already captured, or the same slide
 *     after a share is stopped and started again, is not a new frame.
 *
 * "Changed" is the fraction of grid cells whose brightness differs by more
 * than `pixelDelta`, rather than a mean difference over the whole picture: a
 * new slide with the same template changes a block of text, not the average
 * brightness, and a mean would dilute it.
 *
 * Cells that keep changing are left out of both comparisons. A camera tile
 * beside a share, or a video playing in part of it, changes in most samples;
 * a slide change changes its cells once. A cell that changed in at least
 * `motionCount` of the last `motionWindow` samples is treated as moving, so
 * the strip of cameras next to a shared slide neither stops the slide from
 * settling nor makes the next sample look like a new slide. The fractions are
 * still taken over the whole grid, so the thresholds keep their meaning when
 * part of the picture is masked.
 *
 * How often frames are kept adapts to the screen. A frame must differ from
 * the last kept one by `changeFraction` and be at least an interval after it.
 * The interval starts at `minIntervalMs`; from the third frame kept while the
 * screen keeps changing it steps up through `backoffStepsMs`, and each
 * `quietStepMs` the screen is quiet (holding still, and no different from the
 * last kept frame) steps it back down one. A slide deck is quiet between
 * slides, so every slide on screen for longer than `quietStepMs` and the
 * settle is kept promptly; an application whose content keeps updating is
 * kept at most once per step. A picture that
 * differs by at least `majorChangeFraction` from the one that had settled
 * before it (a different screen in one step, rather than an update of the
 * same one) waits only `minIntervalMs`. That is judged against the settled
 * picture before it rather than the last kept frame, because the small updates
 * of a busy screen add up against an old frame. Two frames are never stamped
 * less than MIN_STAMP_GAP_MS apart.
 *
 * Kept free of I/O so it can be tested on synthetic frames; see
 * videokeyframes.ts for the FFmpeg side.
 */

export interface KeyframeThresholds {
    /** Fraction of cells that must differ from the last kept frame. */
    changeFraction: number;
    /** At most this fraction of cells may differ between consecutive samples for the picture to count as still. */
    stableFraction: number;
    /** Consecutive still samples required before a frame is kept. */
    settleSamples: number;
    /**
     * No frame is kept while more of the grid than this is moving: a gallery
     * of cameras, or a video filling the share, is not a slide however still
     * its borders are.
     */
    maxMovingFraction: number;
    /**
     * A picture that differs this much from the picture that had settled
     * before it is a different screen: it is kept after `minIntervalMs`
     * however far the interval has backed off. Absent: nothing skips the
     * backoff.
     */
    majorChangeFraction?: number;
}

export interface KeyframeDetectorConfig {
    /** Bytes per sample (grid width x height, one byte per cell). */
    sampleBytes: number;
    /** Brightness difference (0-255) above which a cell counts as changed. */
    pixelDelta: number;
    /** Minimum time between two kept frames, and the interval while the screen is quiet. */
    minIntervalMs: number;
    /**
     * The longer intervals the minimum steps up through, one step per frame
     * kept while the screen keeps changing (for example [30000, 60000]).
     * Absent or empty: always `minIntervalMs`.
     */
    backoffStepsMs?: number[];
    /** Each time the screen stays quiet this long, the interval steps back down one. */
    quietStepMs?: number;
    /** Samples of history kept per cell for the motion mask (at most 8). */
    motionWindow?: number;
    /** Changes within the window at which a cell counts as moving. */
    motionCount?: number;
}

/** A kept frame, as the detector holds it; handed back to forget() if it could not be saved. */
export interface KeptFrame {
    readonly frame: Uint8Array;
    readonly atMs: number;
    /** The time the frame is stamped with (KeyframeDecision.firstSeenMs). */
    readonly firstSeenMs: number;
    /** Frames kept since the screen was last quiet, before this one; restored if it is forgotten. */
    readonly burstBefore: number;
}

/**
 * Two kept frames are never stamped closer together than this. With a
 * minimum interval of seconds it only matters for a picture that is kept
 * twice within one still run; see firstSeenMs.
 */
export const MIN_STAMP_GAP_MS = 1000;

/** Kept frames remembered, so undoing one restores the one before it. */
const KEPT_HISTORY = 8;

export interface KeyframeDecision {
    keep: boolean;
    /** The frame that was kept, when `keep` is true. */
    kept?: KeptFrame;
    /** Fraction of cells that differ from the last kept frame (1 when none was kept yet). */
    changeFraction: number;
    /**
     * When the picture that is being kept first appeared: the time of the
     * first sample of the run of still samples. The frame is stamped with this
     * rather than with the time it was confirmed, which is `settleSamples`
     * samples later.
     */
    firstSeenMs: number;
}

/**
 * Fraction of cells whose values differ by more than `pixelDelta`, counting
 * only cells not set in `skip` but dividing by the whole grid.
 */
export function changedFraction(a: Uint8Array, b: Uint8Array, pixelDelta: number, skip?: Uint8Array): number {
    const length = Math.min(a.length, b.length);
    if (length === 0) {
        return 0;
    }
    let changed = 0;
    for (let i = 0; i < length; i++) {
        if (skip && skip[i]) {
            continue;
        }
        if (Math.abs(a[i] - b[i]) > pixelDelta) {
            changed++;
        }
    }
    return changed / length;
}

const popcount8 = (v: number): number => {
    let n = 0;
    for (let x = v; x; x &= x - 1) {
        n++;
    }
    return n;
};

export class KeyframeDetector {
    private readonly config: KeyframeDetectorConfig;
    private previous: Uint8Array | null = null;
    /** The most recent kept frames, newest last; the newest is the reference. */
    private kept: KeptFrame[] = [];
    private stillSamples = 0;
    private stillSinceMs = 0;
    /** The first sample of the current still run. */
    private runStart: Uint8Array | null = null;
    /** The latest sample that had held still for the settle count. */
    private settledPicture: Uint8Array | null = null;
    /** The settled picture from before the current still run began. */
    private runBaseline: Uint8Array | null = null;
    /** The intervals, shortest first: minIntervalMs, then the backoff steps. */
    private readonly intervalsMs: number[];
    private readonly quietStepMs: number;
    /**
     * Frames kept while the screen kept changing, less one for each quiet
     * spell of quietStepMs; at most one more than the number of steps.
     */
    private burst = 0;
    /** The last sample at which the screen moved or differed from the last kept frame. */
    private lastActivityMs: number | null = null;
    /** Per cell, one bit per recent sample: whether the cell changed from the sample before. */
    private readonly history: Uint8Array;
    private readonly moving: Uint8Array;
    private readonly windowMask: number;
    private readonly motionCount: number;

    constructor(config: KeyframeDetectorConfig) {
        this.config = config;
        const window = Math.min(8, Math.max(1, config.motionWindow ?? 6));
        this.windowMask = (1 << window) - 1;
        this.motionCount = Math.max(1, config.motionCount ?? 2);
        this.history = new Uint8Array(config.sampleBytes);
        this.moving = new Uint8Array(config.sampleBytes);
        const steps = (config.backoffStepsMs ?? []).filter((ms) => Number.isFinite(ms) && ms > config.minIntervalMs);
        this.intervalsMs = [config.minIntervalMs, ...steps.sort((a, b) => a - b)];
        this.quietStepMs = config.quietStepMs ?? Infinity;
    }

    /** The interval a frame now has to wait after the last kept one (for diagnostics). */
    get currentIntervalMs(): number {
        return this.intervalsMs[this.stepFor(this.burst)];
    }

    /**
     * The interval after the n-th frame kept while the screen kept changing:
     * the first and second are the minimum apart, the third the first step,
     * and so on, so a single quick change after a slide is still kept promptly.
     */
    private stepFor(burst: number): number {
        return Math.min(Math.max(burst - 1, 0), this.intervalsMs.length - 1);
    }

    /** Fraction of the grid currently treated as moving (for diagnostics). */
    get movingFraction(): number {
        let n = 0;
        for (const m of this.moving) {
            n += m;
        }
        return n / this.moving.length;
    }

    /**
     * Feed one sample. `thresholds` can differ from call to call: the sampler
     * uses stricter ones while the platform gives no screen-share signal.
     */
    push(sample: Uint8Array, nowMs: number, thresholds: KeyframeThresholds): KeyframeDecision {
        if (sample.length !== this.config.sampleBytes) {
            throw new Error(`sample is ${sample.length} bytes, expected ${this.config.sampleBytes}`);
        }
        // Copied: the caller's buffer is a view into a stream chunk that is
        // reused or released after this call.
        const current = Uint8Array.from(sample);
        const { pixelDelta } = this.config;
        // The mask is the cells that were moving before this sample, so a
        // cell's first change -- a slide flipping -- still counts.
        const motion = this.previous ? changedFraction(current, this.previous, pixelDelta, this.moving) : 1;
        // Still against the run's first sample too, so a picture that creeps
        // a little every second does not settle as one run.
        const drift = this.runStart ? changedFraction(current, this.runStart, pixelDelta, this.moving) : 1;
        const still = this.previous !== null && motion <= thresholds.stableFraction && drift <= thresholds.stableFraction;
        if (still) {
            this.stillSamples++;
        } else {
            this.stillSamples = 0;
            this.stillSinceMs = nowMs;
            this.runStart = current;
            this.runBaseline = this.settledPicture;
        }
        // With nothing kept yet, everything that holds still is new. Counting
        // the moving cells as well would keep a wall of moving cameras as the
        // meeting's first "slide", because the mask makes it look still.
        const reference = this.kept.length ? this.kept[this.kept.length - 1] : null;
        const change = reference
            ? changedFraction(current, reference.frame, pixelDelta, this.moving)
            : 1 - this.movingFraction;
        if (this.previous) {
            for (let i = 0; i < current.length; i++) {
                const bit = Math.abs(current[i] - this.previous[i]) > pixelDelta ? 1 : 0;
                this.history[i] = ((this.history[i] << 1) | bit) & this.windowMask;
                this.moving[i] = popcount8(this.history[i]) >= this.motionCount ? 1 : 0;
            }
        }
        this.previous = current;

        // The backoff: each quiet spell of quietStepMs steps the interval back
        // down one. Judged before this sample counts, so the first sample after
        // a long quiet spell (or a pause) is judged at the shorter interval.
        const changed = change >= thresholds.changeFraction;
        if (this.lastActivityMs !== null && nowMs - this.lastActivityMs >= this.quietStepMs) {
            const spells = Math.floor((nowMs - this.lastActivityMs) / this.quietStepMs);
            this.burst = Math.max(0, this.burst - spells);
            this.lastActivityMs += spells * this.quietStepMs;
        }
        if (!still || changed || this.lastActivityMs === null) {
            this.lastActivityMs = nowMs;
        }

        // Stamped with when the picture first appeared, but never before the
        // last kept frame was taken: a picture kept again within the same
        // still run appeared after that frame, not with it.
        const firstSeenMs = reference ? Math.max(this.stillSinceMs, reference.atMs) : this.stillSinceMs;
        const settled = this.stillSamples >= thresholds.settleSamples;
        if (settled) {
            this.settledPicture = current;
        }
        const major =
            this.runBaseline !== null &&
            changedFraction(current, this.runBaseline, pixelDelta, this.moving) >= (thresholds.majorChangeFraction ?? Infinity);
        const intervalMs = major ? this.intervalsMs[0] : this.intervalsMs[this.stepFor(this.burst)];
        const spaced =
            reference === null ||
            (nowMs - reference.atMs >= intervalMs && firstSeenMs - reference.firstSeenMs >= MIN_STAMP_GAP_MS);
        const calm = this.movingFraction <= thresholds.maxMovingFraction;
        const keep = settled && spaced && calm && changed;
        if (!keep) {
            return { keep, changeFraction: change, firstSeenMs };
        }
        const kept: KeptFrame = { frame: current, atMs: nowMs, firstSeenMs, burstBefore: this.burst };
        this.kept.push(kept);
        if (this.kept.length > KEPT_HISTORY) {
            this.kept.shift();
        }
        // The next frames wait longer unless the screen goes quiet first.
        this.burst = Math.min(this.burst + 1, this.intervalsMs.length);
        return { keep, kept, changeFraction: change, firstSeenMs };
    }

    /**
     * Undo a keep, for a frame that could not be saved. The picture stops
     * being a reference, so if it is still on screen it is kept again once it
     * has settled, instead of counting as already captured; the frame kept
     * before it becomes the reference again. Undoing is by identity, so two
     * undos in either order -- a frame skipped while another was being saved,
     * then that one failing -- each remove exactly their own frame.
     */
    forget(kept: KeptFrame): void {
        const index = this.kept.indexOf(kept);
        if (index === -1) {
            return;
        }
        // The newest keep moved the backoff on a step; undoing it moves it back.
        if (index === this.kept.length - 1) {
            this.burst = kept.burstBefore;
        }
        this.kept.splice(index, 1);
        this.stillSamples = 0;
    }

    /**
     * Forget the previous sample and the settle count, keeping the last kept
     * frame and the motion mask. Called when sampling pauses (the share
     * stopped, the meeting was paused), so the first samples after it resumes
     * have to settle again rather than continue a run from before the gap. The
     * last kept frame is kept so a share that resumes on the same slide is not
     * captured twice; the mask is kept because the camera tiles beside a share
     * are usually where they were.
     */
    pause(): void {
        this.previous = null;
        this.runStart = null;
        this.stillSamples = 0;
    }
}
