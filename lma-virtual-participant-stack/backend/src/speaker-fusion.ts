/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Name a row from two signals at once: the meeting page's active-speaker
 * indicator and the transcription engine's voice label.
 *
 * Each signal is wrong in a different way. The page indicator names real
 * people, but it lags and flickers: on a two-person Zoom meeting the speaker
 * view held every switch for about 2.7 s and briefly showed a listener for a
 * backchannel "mm-hmm", so a short reply was named after whoever spoke before
 * it. The voice label never names anybody, but it follows the voice: on that
 * meeting a whole-meeting mapping from label to name would have named 6 of 415
 * rows wrongly, against 37 from the page indicator alone.
 *
 * So every label keeps a time-weighted vote of the page names shown during its
 * speech. Once a label has heard enough speech, and one name holds a clear
 * majority of it, the label is PROVEN and its rows are named after that name
 * whatever the page shows at the moment -- which is also what lets a change of
 * voice name the new row at once, rather than when the page catches up. Until
 * then, and whenever the evidence turns ambiguous, the row takes the page's
 * name exactly as before.
 *
 * Three guards keep the label from doing harm:
 *
 *  - Hysteresis. A label is proven at a higher share than it is kept at, so a
 *    name does not flap as a borderline vote moves by a point.
 *  - Strong contradictions. The page indicator is unreliable on short rows
 *    (its hold and its flickers are a few seconds long) and reliable on long
 *    ones. A row of several seconds that the page gives almost entirely to
 *    somebody other than the label's name is named by the page, and a label
 *    that keeps producing such rows is demoted to page naming: that is what
 *    the diarizer giving one label to two people looks like. A purity vote
 *    alone cannot see it when the merged person speaks little, because the
 *    page's own noise on a correctly separated label is of the same size.
 *  - Enrolled names win. With voice enrollment, the engine reports the name of
 *    somebody who consented to be recognised instead of a voice id. That name
 *    stands as the speaker on its own and takes no part in any vote.
 *
 * A label shared by several people behind ONE page name (a conference room)
 * votes for that one name, so every such label is proven to it and the room's
 * voices still read `Conference Room (spk_0)`, `Conference Room (spk_1)`.
 *
 * Display: a row carries its voice id only under the name the label is
 * attributed to -- the proven name, or while unproven, the name holding most of
 * its vote. Any other row is written with the bare name. So one voice id never
 * appears under two names at once, which is what made one person read as two
 * voices on the Speakers panel.
 *
 * Votes are per engine session: labels are renumbered when a session
 * reconnects, so the caller starts a new instance then. A row already written
 * is never renamed, and an engine re-emitting a final (to correct its text)
 * gets the name the row was first given.
 *
 * The WebSocket transcriber carries a copy of this module for the browser
 * extension's path (calleventdata/speaker-fusion.ts); keep the two in step.
 */

/** A bare voice index, as Amazon Transcribe reports it ("0", "29"). */
const BARE_VOICE_INDEX = /^\d+$/;
const VOICE_ID = /^spk_\d+$/;

/** A voice id in the `spk_N` form; a bare index is prefixed, an enrolled name passes through. */
const normaliseLabel = (raw: string | null | undefined): string | undefined => {
    const trimmed = (raw ?? '').trim();
    if (trimmed.length === 0) {
        return undefined;
    }
    return BARE_VOICE_INDEX.test(trimmed) ? `spk_${trimmed}` : trimmed;
};

/** Whether a label is an engine voice id rather than an enrolled person's name. */
export const isVoiceId = (label: string | undefined): boolean => label !== undefined && VOICE_ID.test(label);

export type FusionThresholds = {
    /** Seconds of page-attributed speech a label needs before it can be proven. */
    minEvidenceSeconds: number;
    /** Share of the label's vote the leading name needs to prove the label. */
    proveShare: number;
    /** Share of the label's vote a proven label's name must keep. */
    keepShare: number;
    /** Rows at least this long can contradict a proven label. */
    contradictionMinRowSeconds: number;
    /** Share of such a row the page must give one other name for it to contradict the label. */
    contradictionShare: number;
    /** Half-life, in seconds of the label's own speech, of its count of contradicting speech. */
    contradictionHalfLifeSeconds: number;
    /** Contradicting seconds (decayed) at which a proven label is demoted. */
    demoteContradictionSeconds: number;
};

/**
 * Chosen on the replay of a 58-minute, two-person Zoom meeting (415 rows); the
 * sweep is summarised in docs/virtual-participant.md. Anything from 5 s to 30 s
 * of evidence and a 0.6-0.8 proving share gave the same result there, so the
 * values sit inside that plateau rather than at an edge tuned to one meeting.
 *
 * The contradiction values come from the same replay. With the two people
 * correctly separated, no row of 2.5 s or more had 80% or more of its span
 * shown as the other person; with one person's speech merged into the other's
 * label, 27 rows did. 3 s leaves a margin above the page's ~2.7 s hold, and
 * 90% (rather than 80%) kept that meeting's labels proven even on the row
 * times recorded before the engine kept its timeline across dropped audio,
 * which ran 1.7 s off the page for the second half of the meeting; on the
 * corrected times both shares caught a simulated merge equally fast.
 *
 * Demotion is final for the engine session. A demoted label's vote is still
 * dominated by its first person, so letting it be proven again as its
 * contradictions decayed handed the merged person's rows back to the wrong
 * name; on the replay, a final demotion brought a simulated merge from 23
 * wrong rows to about the page indicator's own count. The worst a false
 * demotion can do is therefore return that label to page naming, which is
 * how every row was named before this module.
 */
export const DEFAULT_FUSION_THRESHOLDS: FusionThresholds = {
    minEvidenceSeconds: 20,
    proveShare: 0.7,
    keepShare: 0.6,
    contradictionMinRowSeconds: 3,
    contradictionShare: 0.9,
    contradictionHalfLifeSeconds: 120,
    demoteContradictionSeconds: 8,
};

/** How a row was named. */
export type FusionSource = 'enrolled' | 'voice' | 'page';

export type Attribution = {
    /** The row's speaker string: `Name (spk_N)`, a bare name, or an enrolled name. */
    speaker: string;
    /** The person the row is attributed to, without the voice id. */
    name: string;
    source: FusionSource;
};

export type RowToName = {
    /** The engine's voice label for the row: `spk_N`, a bare index, an enrolled name, or nothing. */
    label: string | null | undefined;
    /** The page's name for the row: who it showed for most of the row's span. */
    pageName: string;
    /**
     * Seconds of the row's span the page attributed to each name. Only a final
     * row's evidence is counted. Time the page attributed to nobody (before
     * its first report) is simply absent.
     */
    coverage?: Iterable<[string, number]>;
    /** A partial carries no settled label and adds no evidence. */
    isPartial?: boolean;
    /** A final's row id, so a re-emitted final is named as it was the first time. */
    rowId?: string;
};

type LabelVote = {
    total: number;
    votes: Map<string, number>;
    /** Decayed seconds of strongly contradicting rows, per contradicting name. */
    contradictions: Map<string, number>;
    owner: string | undefined;
    /** Set once a proven label is demoted; it is not proven again in this session. */
    demoted: boolean;
};

/** Rows remembered for re-emitted finals; an engine only re-emits rows of the utterance just closed. */
const REMEMBERED_ROWS = 512;

/** The leading entry of a vote; the earliest-recorded name wins a tie. */
const leaderOf = (votes: Iterable<[string, number]>): [string, number] | undefined => {
    let best: [string, number] | undefined;
    for (const entry of votes) {
        if (best === undefined || entry[1] > best[1]) {
            best = entry;
        }
    }
    return best;
};

/** The finite, positive entries of a coverage map. */
const positive = (coverage: Iterable<[string, number]>): [string, number][] => {
    const kept: [string, number][] = [];
    for (const [name, covered] of coverage) {
        if (covered > 0 && Number.isFinite(covered)) {
            kept.push([name, covered]);
        }
    }
    return kept;
};

export class SpeakerFusion {
    private readonly labels = new Map<string, LabelVote>();
    private readonly named = new Map<string, Attribution>();

    constructor(private readonly thresholds: FusionThresholds = DEFAULT_FUSION_THRESHOLDS) {}

    /** The row's speaker, recording a final's page evidence against its label first. */
    attribute(row: RowToName): Attribution {
        const label = normaliseLabel(row.label);
        if (label === undefined) {
            return { speaker: row.pageName, name: row.pageName, source: 'page' };
        }
        if (!isVoiceId(label)) {
            return { speaker: label, name: label, source: 'enrolled' };
        }
        const final = !row.isPartial;
        if (final && row.rowId !== undefined) {
            const earlier = this.named.get(row.rowId);
            if (earlier !== undefined) {
                return earlier;
            }
        }
        const evidence = final && row.coverage !== undefined ? positive(row.coverage) : [];
        // Judged against the name the label had BEFORE this row, so the row
        // that tips a label into demotion is itself named by the page.
        const contradicted = this.contradictingName(label, evidence);
        if (evidence.length > 0) {
            this.observe(label, evidence, contradicted);
        }
        const attribution =
            contradicted !== undefined ? this.byPage(label, row.pageName) : this.decide(label, row.pageName);
        if (final && row.rowId !== undefined) {
            this.named.set(row.rowId, attribution);
            if (this.named.size > REMEMBERED_ROWS) {
                this.named.delete(this.named.keys().next().value as string);
            }
        }
        return attribution;
    }

    /**
     * The row's speaker as the page names it, whether or not its label is
     * proven. Records no evidence. For the pieces of a row that `attribute`
     * gave to the page -- a row its proven label does not name, such as one
     * the page strongly contradicts -- so that cutting the row by the page does
     * not hand each piece back to the label's proven name.
     */
    attributeByPage(rawLabel: string | null | undefined, pageName: string): Attribution {
        const label = normaliseLabel(rawLabel);
        if (label === undefined) {
            return { speaker: pageName, name: pageName, source: 'page' };
        }
        if (!isVoiceId(label)) {
            return { speaker: label, name: label, source: 'enrolled' };
        }
        return this.byPage(label, pageName);
    }

    /** The name a label is proven to, if any. */
    ownerOf(label: string): string | undefined {
        return this.labels.get(label)?.owner;
    }

    /** The name holding most of a label's vote, proven or not. */
    leadingNameOf(label: string): string | undefined {
        const vote = this.labels.get(label);
        return vote === undefined ? undefined : leaderOf(vote.votes)?.[0];
    }

    private decide(label: string, pageName: string): Attribution {
        const owner = this.ownerOf(label);
        if (owner !== undefined) {
            return { speaker: `${owner} (${label})`, name: owner, source: 'voice' };
        }
        return this.byPage(label, pageName);
    }

    /**
     * The page names the row. The voice id is kept only under the name the
     * label is attributed to, so it never appears under two names; with no
     * evidence yet, the page's name is that name.
     */
    private byPage(label: string, pageName: string): Attribution {
        const attributed = this.ownerOf(label) ?? this.leadingNameOf(label) ?? pageName;
        return {
            speaker: attributed === pageName ? `${pageName} (${label})` : pageName,
            name: pageName,
            source: 'page',
        };
    }

    /** The other name a long row is almost entirely shown as, when its label is proven to someone else. */
    private contradictingName(label: string, evidence: [string, number][]): string | undefined {
        const owner = this.ownerOf(label);
        if (owner === undefined) {
            return undefined;
        }
        const seconds = evidence.reduce((sum, [, covered]) => sum + covered, 0);
        if (seconds < this.thresholds.contradictionMinRowSeconds) {
            return undefined;
        }
        const leader = leaderOf(evidence);
        if (leader === undefined || leader[0] === owner) {
            return undefined;
        }
        return leader[1] / seconds >= this.thresholds.contradictionShare ? leader[0] : undefined;
    }

    private observe(label: string, evidence: [string, number][], contradicted: string | undefined): void {
        let vote = this.labels.get(label);
        if (vote === undefined) {
            vote = { total: 0, votes: new Map(), contradictions: new Map(), owner: undefined, demoted: false };
            this.labels.set(label, vote);
        }
        let seconds = 0;
        for (const [name, covered] of evidence) {
            vote.votes.set(name, (vote.votes.get(name) ?? 0) + covered);
            seconds += covered;
        }
        vote.total += seconds;
        const decay = 0.5 ** (seconds / this.thresholds.contradictionHalfLifeSeconds);
        for (const [name, value] of vote.contradictions) {
            vote.contradictions.set(name, value * decay);
        }
        if (contradicted !== undefined) {
            vote.contradictions.set(contradicted, (vote.contradictions.get(contradicted) ?? 0) + seconds);
        }
        this.settle(vote);
    }

    /** Prove, keep or demote a label after new evidence. */
    private settle(vote: LabelVote): void {
        const t = this.thresholds;
        const share = (name: string): number => (vote.votes.get(name) ?? 0) / vote.total;
        const contradiction = Math.max(0, ...vote.contradictions.values());
        if (vote.owner !== undefined) {
            if (share(vote.owner) < t.keepShare || contradiction >= t.demoteContradictionSeconds) {
                vote.owner = undefined;
                vote.demoted = true;
            }
            return;
        }
        if (!vote.demoted && vote.total >= t.minEvidenceSeconds) {
            const leader = leaderOf(vote.votes)?.[0];
            if (leader !== undefined && share(leader) >= t.proveShare) {
                vote.owner = leader;
            }
        }
    }
}
