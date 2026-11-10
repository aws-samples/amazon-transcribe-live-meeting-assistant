/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { KeyframeDetector, KeyframeThresholds, MIN_STAMP_GAP_MS, changedFraction } from './keyframe-detector.js';

const W = 64;
const H = 36;
const N = W * H;
const SHARE: KeyframeThresholds = { changeFraction: 0.03, stableFraction: 0.02, settleSamples: 2, maxMovingFraction: 0.5 };
const STRICT: KeyframeThresholds = { changeFraction: 0.04, stableFraction: 0.01, settleSamples: 3, maxMovingFraction: 0.25 };

/**
 * A "slide": a light background with dark text rows. Words are runs of dark
 * cells whose positions depend on the seed, so two slides share a template and
 * differ in a few percent of the grid, as real ones do.
 */
function slide(seed: number): Uint8Array {
    const s = new Uint8Array(N).fill(230);
    for (let row = 4; row < H - 2; row += 4) {
        let x = 4;
        let k = seed * 31 + row;
        while (x < W - 16) {
            const len = 2 + (k % 5);
            for (let i = 0; i < len; i++) s[row * W + x + i] = 40;
            x += len + 1 + ((k >> 2) % 3);
            k = (k * 1103515245 + 12345) & 0x7fffffff;
        }
    }
    return s;
}

/** The same picture with a moving block in a corner (a camera tile). */
function withMotion(base: Uint8Array, t: number, size = 6): Uint8Array {
    const s = Uint8Array.from(base);
    for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) s[y * W + (W - size + x)] = (t * 53 + x * 31 + y * 17) % 256;
    }
    return s;
}

const detector = () => new KeyframeDetector({ sampleBytes: N, pixelDelta: 12, minIntervalMs: 4000 });

test('changedFraction counts cells beyond the delta, not the mean difference', () => {
    const a = new Uint8Array(100).fill(100);
    const b = Uint8Array.from(a);
    for (let i = 0; i < 10; i++) b[i] = 200; // 10% of cells changed a lot
    for (let i = 10; i < 100; i++) b[i] = 110; // the rest changed a little
    assert.equal(changedFraction(a, b, 16), 0.1);
    assert.equal(changedFraction(a, a, 16), 0);
});

test('a new picture is kept only once it has held still for the settle count', () => {
    const d = detector();
    const s1 = slide(1);
    // First sample: nothing to compare motion against yet.
    assert.equal(d.push(s1, 0, SHARE).keep, false);
    assert.equal(d.push(s1, 1000, SHARE).keep, false); // still 1
    const kept = d.push(s1, 2000, SHARE); // still 2 -> kept
    assert.equal(kept.keep, true);
    assert.equal(kept.changeFraction, 1, 'the first frame of a meeting counts as wholly new');
    assert.equal(kept.firstSeenMs, 0, 'stamped with when the picture first appeared');
    // Holding on the same slide keeps nothing more.
    for (let t = 3000; t < 20000; t += 1000) assert.equal(d.push(s1, t, SHARE).keep, false);
});

test('a slide change is kept after it settles, stamped with when it appeared', () => {
    const d = detector();
    const s1 = slide(1);
    const s2 = slide(2);
    [0, 1000, 2000].forEach((t) => d.push(s1, t, SHARE));
    const diff = changedFraction(s1, s2, 12);
    assert.ok(diff > SHARE.changeFraction && diff < 0.3, `fixture: the slides differ by a few percent (${diff})`);
    assert.equal(d.push(s2, 10000, SHARE).keep, false);
    assert.equal(d.push(s2, 11000, SHARE).keep, false);
    const kept = d.push(s2, 12000, SHARE);
    assert.equal(kept.keep, true);
    assert.equal(kept.firstSeenMs, 10000);
});

test('a moving picture is never kept until it stops', () => {
    const d = detector();
    // Every sample differs completely from the last (a video playing).
    for (let t = 0; t < 30; t++) {
        const noisy = new Uint8Array(N).map((_, i) => (i * 37 + t * 101) % 256);
        assert.equal(d.push(noisy, t * 1000, SHARE).keep, false);
    }
});

test('a moving camera strip beside the share neither blocks settling nor counts as a change', () => {
    const d = detector();
    const s1 = slide(1);
    const s2 = slide(2);
    // A 12x12 tile is 6% of the grid: more than the stillness allowance, so
    // without the motion mask the slide would never count as still.
    const kept: number[] = [];
    for (let t = 0; t < 40; t++) {
        if (d.push(withMotion(t < 20 ? s1 : s2, t, 12), t * 1000, SHARE).keep) kept.push(t);
    }
    assert.equal(kept.length, 2, `one frame per slide, got frames at ${kept}`);
    assert.ok(kept[1] >= 20 && kept[1] <= 23, 'the second slide is kept shortly after it appears');
    assert.ok(d.movingFraction > 0.05, 'the tile is masked as moving');
});

test('a gallery of moving cameras produces no frames, though its borders hold still', () => {
    for (const thresholds of [SHARE, STRICT]) {
        const d = detector();
        let kept = 0;
        for (let t = 0; t < 60; t++) {
            const s = new Uint8Array(N).fill(60);
            for (let tile = 0; tile < 6; tile++) {
                const ox = (tile % 3) * 21;
                const oy = Math.floor(tile / 3) * 18;
                for (let y = 2; y < 16; y++) {
                    for (let x = 2; x < 19; x++) s[(oy + y) * W + ox + x] = (t * 23 + tile * 40 + x * 9 + y * 5) % 256;
                }
            }
            if (d.push(s, t * 1000, thresholds).keep) kept++;
        }
        assert.ok(d.movingFraction > 0.5, 'the tiles are masked');
        assert.equal(kept, 0);
    }
});

test('returning to a slide that was the last one kept is not a new frame', () => {
    const d = detector();
    const s1 = slide(1);
    [0, 1000, 2000].forEach((t) => d.push(s1, t, SHARE));
    d.pause(); // share stopped
    // Share resumes on the same slide.
    const results = [10000, 11000, 12000, 13000].map((t) => d.push(s1, t, SHARE).keep);
    assert.deepEqual(results, [false, false, false, false]);
});

test('after a pause a resumed share has to settle again', () => {
    const d = detector();
    const s1 = slide(1);
    const s2 = slide(2);
    [0, 1000, 2000].forEach((t) => d.push(s1, t, SHARE));
    d.push(s2, 9000, SHARE);
    d.push(s2, 10000, SHARE);
    d.pause();
    assert.equal(d.push(s2, 20000, SHARE).keep, false, 'one sample after a pause is not settled');
    assert.equal(d.push(s2, 21000, SHARE).keep, false);
    assert.equal(d.push(s2, 22000, SHARE).keep, true);
});

test('frames closer together than the minimum interval are not kept', () => {
    const d = detector();
    const s1 = slide(1);
    const s2 = slide(2);
    [0, 1000, 2000].forEach((t) => d.push(s1, t, SHARE)); // kept at 2000
    // s2 settles at 4000-5000 but the interval runs to 6000.
    assert.equal(d.push(s2, 3000, SHARE).keep, false);
    assert.equal(d.push(s2, 4000, SHARE).keep, false);
    assert.equal(d.push(s2, 5000, SHARE).keep, false);
    assert.equal(d.push(s2, 6000, SHARE).keep, true, 'kept once the interval has passed, if still new');
});

test('stricter thresholds ignore a small change that the share thresholds keep', () => {
    const base = slide(1);
    const small = Uint8Array.from(base);
    for (let i = 0; i < Math.round(N * 0.035); i++) small[N - 1 - i] = 0; // 3.5% of the picture changes
    const run = (thresholds: KeyframeThresholds) => {
        const d = detector();
        let kept = 0;
        let t = 0;
        for (const s of [base, small]) {
            for (let k = 0; k < 6; k++) if (d.push(s, (t += 1000), thresholds).keep) kept++;
            t += 5000;
        }
        return kept;
    };
    assert.equal(run(SHARE), 2);
    assert.equal(run(STRICT), 1);
});

test('the strict thresholds need a longer and stiller settle', () => {
    const d = detector();
    const s1 = slide(1);
    const results = [0, 1000, 2000, 3000].map((t) => d.push(s1, t, STRICT).keep);
    assert.deepEqual(results, [false, false, false, true]);
});

test('forgetting a keep makes the same picture new again, and restores the one before', () => {
    const d = detector();
    const s1 = slide(1);
    const s2 = slide(2);
    [0, 1000, 2000].forEach((t) => d.push(s1, t, SHARE)); // s1 kept
    const second = [9000, 10000, 11000].map((t) => d.push(s2, t, SHARE))[2];
    assert.ok(second.kept, 's2 kept');
    d.forget(second.kept!); // s2 was not saved
    const again = [12000, 13000, 14000].map((t) => d.push(s2, t, SHARE));
    // The settle count starts again: two more still samples.
    assert.deepEqual(again.map((r) => r.keep), [false, true, false], 'kept again after settling, once');
    assert.ok(again[1].changeFraction > SHARE.changeFraction, 'judged against s1, the frame before');
});

test('undoing two keeps in either order removes exactly those two', () => {
    const d = detector();
    const [s1, s2, s3] = [slide(1), slide(2), slide(3)];
    const keep = (s: Uint8Array, t0: number) => [t0, t0 + 1000, t0 + 2000].map((t) => d.push(s, t, SHARE))[2].kept!;
    keep(s1, 0);
    const k2 = keep(s2, 10000);
    const k3 = keep(s3, 20000);
    d.forget(k3); // skipped while k2 was being saved
    d.forget(k2); // then k2 failed
    d.forget(k2); // a repeated undo does nothing
    // s1 is the reference again: s1 on screen is not new, s2 is.
    const onS1 = [30000, 31000, 32000].map((t) => d.push(s1, t, SHARE).keep);
    assert.deepEqual(onS1, [false, false, false]);
    const onS2 = [40000, 41000, 42000].map((t) => d.push(s2, t, SHARE).keep);
    assert.deepEqual(onS2, [false, false, true]);
});

test('a sample of the wrong size is rejected', () => {
    assert.throws(() => detector().push(new Uint8Array(10), 0, SHARE), /expected 2304/);
});

// The sampler's backoff, as shipped (keyframe-sampler.ts).
const BACKOFF = { sampleBytes: N, pixelDelta: 12, minIntervalMs: 4000, backoffStepsMs: [30000, 60000], quietStepMs: 10000 };
const SHARE_MAJOR: KeyframeThresholds = { ...SHARE, majorChangeFraction: 0.1 };
const backoffDetector = () => new KeyframeDetector(BACKOFF);

/**
 * A live application on the shared screen: every three seconds one of eight
 * blocks of the page is redrawn (5% of the grid), then holds still. Each
 * update differs from the picture before by more than the share threshold and
 * less than a change of screen, which is what a page that keeps updating
 * scored on a measured meeting (3-9%). The blocks take turns, so no cell
 * changes often enough to be masked as moving.
 */
function livePage() {
    const page = slide(1);
    let update = 0;
    return (t: number): Uint8Array => {
        while (update <= Math.floor(t / 3)) {
            const block = update % 8;
            const value = Math.floor(update / 8) % 2 ? 230 : 90;
            const ox = 2 + (block % 4) * 15;
            const oy = 2 + Math.floor(block / 4) * 17;
            for (let y = 0; y < 10; y++) for (let x = 0; x < 12; x++) page[(oy + y) * W + ox + x] = value;
            update++;
        }
        return Uint8Array.from(page);
    };
}

/** The times (seconds) frames are kept over `seconds` of a picture, one sample a second. */
function keptTimes(d: KeyframeDetector, picture: (t: number) => Uint8Array, seconds: number, t0 = 0, thresholds = SHARE_MAJOR) {
    const kept: { at: number; firstSeen: number; change: number }[] = [];
    for (let t = t0; t < t0 + seconds; t++) {
        const r = d.push(picture(t), t * 1000, thresholds);
        if (r.keep) kept.push({ at: t, firstSeen: r.firstSeenMs / 1000, change: r.changeFraction });
    }
    return kept;
}

test('a screen whose content keeps changing slightly yields at most one frame per backoff interval', () => {
    // The fixture changes as a live page does: each update by more than the
    // threshold, less than a new screen.
    const page = livePage();
    const pictures = Array.from({ length: 600 }, (_, t) => page(t));
    for (let t = 3; t < 600; t += 3) {
        const update = changedFraction(pictures[t], pictures[t - 1], 12);
        assert.ok(update >= 0.03 && update < 0.1, `update at ${t}s scored ${update}`);
    }
    const kept = keptTimes(backoffDetector(), (t) => pictures[t], 600);
    const gaps = kept.slice(1).map((k, i) => k.at - kept[i].at);
    assert.ok(gaps[0] >= 4, `first gap ${gaps[0]}`);
    assert.ok(gaps[1] >= 30, `second gap ${gaps[1]}`);
    gaps.slice(2).forEach((g) => assert.ok(g >= 60, `gap of ${g}s once backed off: ${gaps}`));
    // Ten minutes: the first frame, one 4 s and one 30 s later, then one a minute.
    assert.ok(kept.length <= 12, `${kept.length} frames`);

    // Without the backoff the same screen gives a frame every few seconds,
    // which is what the measured meeting showed.
    const unbounded = keptTimes(detector(), livePage(), 600);
    assert.ok(unbounded.length >= 5 * kept.length, `${unbounded.length} frames without the backoff`);
});

test('a slide change after a quiet period yields a frame promptly', () => {
    const d = backoffDetector();
    const busy = livePage();
    keptTimes(d, busy, 300); // backed off to a minute
    assert.equal(d.currentIntervalMs, 60000);
    // The page stops changing and stays on screen (its last update is kept
    // within the minute), then the presenter moves to a similar slide.
    const held = busy(299);
    keptTimes(d, () => held, 90, 300);
    assert.equal(d.currentIntervalMs, 4000, 'quiet for longer than the reset: back to the shortest interval');
    const next = Uint8Array.from(held);
    for (let i = 0; i < Math.round(N * 0.05); i++) next[N - 1 - i] = 0; // 5%: a template slide's change
    const kept = keptTimes(d, () => next, 10, 390);
    assert.equal(kept.length, 1);
    assert.ok(kept[0].at <= 392, `kept at ${kept[0].at}, two settle samples after it appeared at 390`);
    assert.equal(kept[0].firstSeen, 390);
});

test('a change of screen while backed off waits only the minimum interval', () => {
    const d = backoffDetector();
    const busy = livePage();
    const before = keptTimes(d, busy, 300);
    assert.equal(d.currentIntervalMs, 60000);
    const last = before[before.length - 1].at;
    const other = slide(7).map((v, i) => (i % 3 ? v : 255 - v)); // a different screen
    const kept = keptTimes(d, () => other, 20, last + 1);
    assert.equal(kept.length, 1);
    assert.ok(kept[0].change >= 0.1);
    assert.ok(kept[0].at - last <= 6, `kept ${kept[0].at - last}s after the last frame`);
});

test('a slide deck with a slide every 12 seconds keeps every slide promptly', () => {
    // Slides of one template: each differs from the one before by a few
    // percent, below a change of screen, so none skips the backoff.
    for (let n = 1; n < 20; n++) assert.ok(changedFraction(slide(n), slide(n + 1), 12) < 0.1, `slide ${n}`);
    const d = backoffDetector();
    const kept = keptTimes(d, (t) => slide(1 + Math.floor(t / 12)), 240);
    assert.equal(kept.length, 20);
    kept.forEach((k, i) => {
        assert.equal(k.firstSeen, i * 12);
        assert.ok(k.at <= i * 12 + 2, `slide ${i + 1} kept at ${k.at}`);
    });
});

test('after a busy spell each quiet spell steps the interval back down one', () => {
    const d = backoffDetector();
    const busy = livePage();
    keptTimes(d, busy, 300);
    assert.equal(d.currentIntervalMs, 60000);
    const held = busy(299);
    // Its last update is kept within the minute; from then on it is quiet.
    let t = 300;
    while (!d.push(held, t * 1000, SHARE_MAJOR).keep) t++;
    assert.ok(t < 360, `the last update kept at ${t}`);
    assert.equal(d.currentIntervalMs, 60000);
    const intervals: number[] = [];
    for (let s = 1; s <= 20; s++) {
        d.push(held, (t + s) * 1000, SHARE_MAJOR);
        if (s % 10 === 0) intervals.push(d.currentIntervalMs);
    }
    assert.deepEqual(intervals, [30000, 4000]);
});

test('no two frames within one second', () => {
    // A picture that creeps by 1% a second: less than the stillness allowance
    // between samples, so judged sample to sample it is one long still run.
    // Frames kept from that run were all stamped with its start.
    const creeping = (t: number) => {
        const s = slide(1);
        for (let i = 0; i < Math.round(N * 0.01) * t && i < N; i++) s[(i * 7919) % N] = s[(i * 7919) % N] > 128 ? 0 : 255;
        return s;
    };
    for (const d of [detector(), backoffDetector()]) {
        const kept = keptTimes(d, creeping, 120);
        assert.ok(kept.length >= 2, `fixture: ${kept.length} frames`);
        kept.slice(1).forEach((k, i) => {
            assert.ok(k.firstSeen - kept[i].firstSeen >= 1, `stamped ${k.firstSeen} after ${kept[i].firstSeen}`);
        });
    }
    // Nor with no minimum interval at all, for pictures that settle quickly.
    const fast = new KeyframeDetector({ sampleBytes: N, pixelDelta: 12, minIntervalMs: 0 });
    const quick: KeyframeThresholds = { ...SHARE, settleSamples: 1 };
    const stamps: number[] = [];
    for (let t = 0; t < 5000; t += 100) {
        const r = fast.push(slide(1 + Math.floor(t / 200)), t, quick);
        if (r.keep) stamps.push(r.firstSeenMs);
    }
    assert.ok(stamps.length >= 3, `fixture: ${stamps.length} frames`);
    stamps.slice(1).forEach((s, i) => assert.ok(s - stamps[i] >= MIN_STAMP_GAP_MS, `${s} after ${stamps[i]}`));
});

test('forgetting a keep undoes its step of the backoff', () => {
    const d = backoffDetector();
    [0, 1000, 2000].forEach((t) => d.push(slide(1), t, SHARE_MAJOR));
    assert.equal(d.currentIntervalMs, 4000, 'one quick change after a frame is still kept promptly');
    const second = [6000, 7000, 8000].map((t) => d.push(slide(2), t, SHARE_MAJOR))[2];
    assert.ok(second.kept);
    assert.equal(d.currentIntervalMs, 30000, 'a third frame in a row waits for the first step');
    d.forget(second.kept!); // not saved
    assert.equal(d.currentIntervalMs, 4000);
});
