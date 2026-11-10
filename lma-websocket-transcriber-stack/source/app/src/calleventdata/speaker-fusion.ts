/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Name a meeting-audio row from two signals at once: the meeting page's
 * active-speaker name, which the browser extension reports in SPEAKER_CHANGE
 * frames, and the transcription engine's voice label.
 *
 * This is a copy of the Virtual Participant's speaker-fusion.ts, which reads
 * the same meeting pages for the same purpose; the explanation of the vote,
 * its guards and its thresholds lives there, and the two must be kept in
 * step. The parts only this path needs are at the end: a timeline of the
 * SPEAKER_CHANGE frames on the meeting's audio clock, and the per-meeting
 * state that holds it.
 *
 * In short: every voice label keeps a time-weighted vote of the page names
 * shown during its speech. Once a label has enough speech and one name holds
 * a clear majority, its rows are named after that name whatever the page
 * shows at the moment, so a short reply is not named after whoever the page
 * is still showing. Until then, and once a label starts to carry a second
 * person, rows are named by the page. An enrolled voice's name always wins.
 *
 * Pure: reads no environment at import time, so it stays importable by the
 * offline unit tests (same constraint as diarization.ts).
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
const positive = (coverage: Iterable<[string, number]>): Array<[string, number]> => {
    const kept: Array<[string, number]> = [];
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
    private contradictingName(label: string, evidence: Array<[string, number]>): string | undefined {
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

    private observe(label: string, evidence: Array<[string, number]>, contradicted: string | undefined): void {
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

// ── The page's speaker changes on the meeting's audio clock ───────────────────

/**
 * Who the meeting page showed as speaking at each point of the meeting's
 * audio clock (session-lifecycle.ts recordedAudioSeconds, which both engines'
 * row times are on).
 *
 * A SPEAKER_CHANGE frame arrives on the same connection as the audio, in
 * order, and the extension sends audio as it is captured, so the audio
 * received when a change arrives is where the change happened, give or take
 * the page's own delay in showing it. Rows used to be named after whoever was
 * active when the row was WRITTEN, a second or more after its audio, which gave
 * the start of every reply to the previous speaker.
 *
 * The VP's roster-timeline.ts is the original; this is the same lookup.
 * As there, a report of nobody speaking is not recorded, so the last person
 * shown carries on: the extension's Teams reader sends `n/a` for it.
 */
const NOBODY = new Set(['none', 'n/a']);

export class PageSpeakerTimeline {
    private readonly times: number[] = [];
    private readonly names: string[] = [];

    record(atSeconds: number, name: string | undefined): void {
        const trimmed = (name ?? '').trim();
        if (trimmed.length === 0 || NOBODY.has(trimmed.toLowerCase())) {
            return;
        }
        const last = this.times.length > 0 ? this.times[this.times.length - 1] : -Infinity;
        this.times.push(Math.max(atSeconds, last));
        this.names.push(trimmed);
    }

    /** True until the page has reported anybody. */
    get isEmpty(): boolean {
        return this.times.length === 0;
    }

    /**
     * The name shown for most of [startSec, endSec], with every change moved
     * back by `lagSeconds`. A span no change overlaps takes the name in effect
     * at its start, or the first one.
     */
    speakerFor(startSec: number, endSec: number, lagSeconds = 0): string | undefined {
        if (this.times.length === 0) {
            return undefined;
        }
        const { covered, inEffect } = this.cover(startSec, endSec, lagSeconds);
        const leader = leaderOf(covered);
        if (leader !== undefined) {
            return leader[0];
        }
        return inEffect < 0 ? this.names[0] : this.names[inEffect];
    }

    /** Seconds of the span each name was shown for. Time before the first report belongs to nobody. */
    coverage(startSec: number, endSec: number, lagSeconds = 0): Map<string, number> {
        return this.times.length === 0 ? new Map() : this.cover(startSec, endSec, lagSeconds).covered;
    }

    private cover(
        startSec: number,
        endSec: number,
        lagSeconds: number
    ): { covered: Map<string, number>; inEffect: number } {
        const count = this.times.length;
        const end = Math.max(endSec, startSec);
        const shifted = (i: number): number => this.times[i] - lagSeconds;
        let lo = 0;
        let hi = count;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (shifted(mid) <= startSec) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        const inEffect = lo - 1;
        const covered = new Map<string, number>();
        for (let i = Math.max(inEffect, 0); i < count && shifted(i) < end; i += 1) {
            const from = Math.max(shifted(i), startSec);
            const to = Math.min(i + 1 < count ? shifted(i + 1) : Infinity, end);
            if (to > from) {
                covered.set(this.names[i], (covered.get(this.names[i]) ?? 0) + (to - from));
            }
        }
        return { covered, inEffect };
    }
}

/**
 * How late the page shows a change of speaker, in seconds.
 *
 * The Virtual Participant's default for meeting pages other than the Zoom web
 * client, which it measured as reporting changes as they happen. The frames
 * carry no platform, so one value covers every page; it has not been measured
 * on the extension's path. SPEAKER_CHANGE_LAG_SECONDS overrides it.
 */
export const DEFAULT_PAGE_SPEAKER_LAG_SECONDS = 0.75;

export const pageSpeakerLagSeconds = (raw: string | undefined = process.env['SPEAKER_CHANGE_LAG_SECONDS']): number => {
    const configured = Number.parseFloat(raw ?? '');
    return Number.isFinite(configured) && configured >= 0 ? configured : DEFAULT_PAGE_SPEAKER_LAG_SECONDS;
};

/** One meeting-audio row to name. */
export type MeetingRow = {
    /** The engine session that produced it: voice labels mean one voice only within one session. */
    session: string;
    label: string | null | undefined;
    startTime: number;
    endTime: number;
    isPartial: boolean;
    /** The name to use when the page has reported nobody: the client's own label for the channel. */
    fallbackName: string;
    rowId?: string;
};

/**
 * A meeting's page speakers and the vote over its meeting-audio voice labels.
 *
 * Held per meeting rather than per engine session, because the timeline is on
 * the meeting's audio clock and outlives a reconnect; the vote restarts with
 * each engine session.
 */
export class PageSpeakers {
    readonly timeline = new PageSpeakerTimeline();

    private fusion = new SpeakerFusion();

    private fusionSession: string | undefined;

    constructor(private readonly lagSeconds: number = pageSpeakerLagSeconds()) {}

    /** Record the page's report of who is speaking, at the meeting's audio clock. */
    record(atSeconds: number, name: string | undefined): void {
        this.timeline.record(atSeconds, name);
    }

    /**
     * The row's speaker. With no SPEAKER_CHANGE ever received (Stream Audio,
     * a client that does not read the page) nothing is voted on, and a row is
     * named exactly as before: the channel's label, plus the voice id.
     */
    nameRow(row: MeetingRow): Attribution {
        const fusion = this.useSession(row.session);
        const pageName = this.pageNameFor(row.startTime, row.endTime) ?? row.fallbackName;
        return fusion.attribute({
            label: row.label,
            pageName,
            coverage: row.isPartial
                ? undefined
                : this.timeline.coverage(row.startTime, row.endTime, this.lagSeconds),
            isPartial: row.isPartial,
            rowId: row.rowId,
        });
    }

    /** The name the page showed for most of a span, or undefined before its first report. */
    pageNameFor(startTime: number, endTime: number): string | undefined {
        return this.timeline.speakerFor(startTime, endTime, this.lagSeconds);
    }

    /** The vote for `session`, starting a new one unless it is the current session. */
    useSession(session: string): SpeakerFusion {
        if (session !== this.fusionSession) {
            this.fusion = new SpeakerFusion();
            this.fusionSession = session;
        }
        return this.fusion;
    }
}

/**
 * Keyed by the meeting's call metadata, which lives as long as the meeting's
 * session (and across a client reconnect), without adding a field that would
 * be written into every log line that prints the metadata.
 */
const perMeeting = new WeakMap<object, PageSpeakers>();

export const pageSpeakersFor = (callMetadata: object): PageSpeakers => {
    let speakers = perMeeting.get(callMetadata);
    if (speakers === undefined) {
        speakers = new PageSpeakers();
        perMeeting.set(callMetadata, speakers);
    }
    return speakers;
};
