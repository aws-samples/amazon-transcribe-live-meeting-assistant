/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which of the voice assistant's replies the meeting hears, while it is
 * activated by wake phrase in assistant (`normal`) mode.
 *
 * An activated Nova Sonic session hears the whole meeting, and the model
 * answers every turn it hears: it has no way of telling a question put to it
 * from two participants talking to each other. Recorded on a live Zoom meeting
 * with a 30-second idle window: one wake phrase, and then two and a half
 * minutes of the assistant answering the conversation around it ("I
 * understand what you're describing", "Okay", "Okay, noted", "It seems like
 * you're referring to something specific in the chat") until the window
 * finally lapsed. Each of those answers was played, so each one counted as the
 * assistant being busy and held the window open for the next.
 *
 * The prompt asks the model to stay quiet when it is not addressed, but the
 * guarantee is here: a reply is let through only when the turn it answers was
 * addressed to the assistant. That is
 *
 * - the request the wake phrase activated it with (sent as text), or a later
 *   turn that contains the wake phrase or addresses the assistant by its name
 *   (voice-addressing.ts has the rule for the name);
 * - a turn that is addressed only together with the turn before it, which
 *   ended within UTTERANCE_GAP_MS: one utterance that Nova Sonic's turn
 *   detection split so that the address straddles the two turns
 *   (`split-utterance`);
 * - a turn heard within UTTERANCE_GAP_MS of the participants' own transcript
 *   (Amazon Transcribe or the ASR MicroVM) finalising an utterance that
 *   addresses the assistant (`transcript-addressed`). That transcript sees the
 *   whole utterance with punctuation where Nova sees fragments, so it is the
 *   better unit. It arrives either before Nova's turn, which is then answered,
 *   or after it, in which case the withheld reply is released if nothing of it
 *   has been withheld yet: no audio dropped and no lookup refused (the agent
 *   decides that; see noteAddressedTranscript);
 * - a turn that follows an addressed one within ADDRESSED_FOLLOW_ON_MS, which
 *   is the same request split by a pause ("Hey Alex ... what's on the agenda");
 * - any turn while the answer to a lookup an addressed request started is
 *   still to come. The participants go on talking while they wait, and on the
 *   live meeting the model delivered the lookup's answer as its reply to one
 *   of those turns, so withholding it would have withheld the answer.
 *
 * Every other turn withholds the reply to it: its audio is dropped and its text
 * kept out of the transcript and the history, the same treatment an unaddressed
 * `set_mode` reply already gets. The reply is everything the model says until
 * the next participant turn, not only its first audio block: a lookup the model
 * asks for in that reply is refused (muted-lookup.ts `unaddressed-turn`), and
 * what it says after the refusal's tool result ("Let me search for that",
 * "Sorry, I can't look that up") is part of the same withheld reply. Answering
 * and refusing are never mixed: a turn's lookup runs exactly when its reply is
 * heard. A withheld reply plays nothing, so it does not
 * hold the activation window open either, and the window lapses on its own
 * idle timeout.
 *
 * Once an answer has been heard in full, nothing more is owed: the next reply
 * needs the next addressed turn. A participant who talks over the assistant
 * without addressing it ends the answer the same way, because the reply the
 * model produces next is to the interruption.
 */

import { UTTERANCE_GAP_MS } from './voice-addressing.js';

/** How long after an addressed turn a following turn still counts as part of
 *  the same request. Nova Sonic's turn detection splits an utterance at a
 *  pause of well under a second; a participant who has finished asking and
 *  starts talking to the room usually leaves longer than this. */
export const ADDRESSED_FOLLOW_ON_MS = 4000;

/** How close two transcripts of one participant's speech must be to be the
 *  same utterance (voice-addressing.ts). */
export { UTTERANCE_GAP_MS };

/** Why a turn's reply is, or is not, let through. */
export type ReplyGateReason =
  | 'addressed'
  | 'split-utterance'
  | 'transcript-addressed'
  | 'follow-on'
  | 'lookup-pending'
  | 'not-addressed';

export interface ReplyGateDecision {
  audible: boolean;
  reason: ReplyGateReason;
}

export class WakeReplyGate {
  /** An addressed request has not been answered yet. */
  private owed = false;
  /** When the latest addressed turn (or its follow-on) ended. */
  private lastAddressedAt = 0;
  /** A lookup was started and its answer has not been heard yet. */
  private lookupPending = false;
  /** The latest participant turn: its text, when it ended, and whether its reply was withheld. */
  private lastTurn: { text: string; at: number; withheld: boolean } | null = null;
  /** When the participants' transcript last finalised an utterance addressed to the assistant. */
  private transcriptAddressedAt = 0;

  constructor(
    private readonly followOnMs: number = ADDRESSED_FOLLOW_ON_MS,
    private readonly utteranceGapMs: number = UTTERANCE_GAP_MS,
  ) {}

  /** Whether an answer is still owed to an addressed request. */
  answerOwed(): boolean {
    return this.owed || this.lookupPending;
  }

  /** A lookup is being run for the reply in progress. Its answer is owed. */
  noteLookupStarted(): void {
    this.lookupPending = true;
  }

  /** The assistant was activated by wake phrase with a request to answer. */
  noteActivated(now: number = Date.now()): void {
    this.owed = true;
    this.lastAddressedAt = now;
  }

  /**
   * A participant's turn, as the model transcribed it, has ended. Decides
   * whether the reply to it is heard.
   *
   * @param addressed The turn contains the wake phrase.
   */
  noteUserTurn(addressed: boolean, now: number = Date.now()): ReplyGateDecision {
    return this.decideTurn(addressed ? 'addressed' : null, '', now);
  }

  /**
   * A participant's turn, as the model transcribed it, has ended: decided over
   * the utterance it belongs to rather than the turn alone. `addresses` is
   * asked about the turn, then about the turn together with the one before it
   * when that one ended within the utterance gap.
   */
  noteUserUtterance(
    text: string,
    addresses: (utterance: string) => boolean,
    now: number = Date.now(),
  ): ReplyGateDecision {
    let reason: ReplyGateReason | null = null;
    if (addresses(text)) {
      reason = 'addressed';
    } else if (
      this.lastTurn
      && now - this.lastTurn.at <= this.utteranceGapMs
      && addresses(`${this.lastTurn.text} ${text}`)
    ) {
      reason = 'split-utterance';
    }
    return this.decideTurn(reason, text, now);
  }

  /**
   * The participants' transcript has finalised an utterance that addresses the
   * assistant. A Nova turn heard within the utterance gap after this is that
   * utterance, and is answered. Returns true when the latest Nova turn,
   * withheld, ended within the gap before it, so is that utterance too: the
   * reply to it is now owed, and the caller lets it through if nothing of it
   * has been withheld yet.
   *
   * @param releasable Nothing of the withheld reply has been dropped or
   *   refused yet. When something has, the reply stays withheld: half an answer,
   *   or an answer after a refused lookup, is worse than none.
   */
  noteAddressedTranscript(releasable: boolean, now: number = Date.now()): boolean {
    this.transcriptAddressedAt = now;
    const turn = this.lastTurn;
    if (!releasable || !turn || !turn.withheld || now - turn.at > this.utteranceGapMs) return false;
    turn.withheld = false;
    this.owed = true;
    this.lastAddressedAt = now;
    return true;
  }

  private decideTurn(addressedBy: ReplyGateReason | null, text: string, now: number): ReplyGateDecision {
    let decision: ReplyGateDecision;
    if (addressedBy) {
      decision = { audible: true, reason: addressedBy };
    } else if (this.transcriptAddressedAt > 0 && now - this.transcriptAddressedAt <= this.utteranceGapMs) {
      decision = { audible: true, reason: 'transcript-addressed' };
    } else if (this.lastAddressedAt > 0 && now - this.lastAddressedAt <= this.followOnMs) {
      decision = { audible: true, reason: 'follow-on' };
    } else if (this.lookupPending) {
      decision = { audible: true, reason: 'lookup-pending' };
    } else {
      decision = { audible: false, reason: 'not-addressed' };
    }
    this.lastTurn = { text, at: now, withheld: !decision.audible };
    if (decision.reason === 'lookup-pending') return decision;
    this.owed = decision.audible;
    if (decision.audible) this.lastAddressedAt = now;
    return decision;
  }

  /**
   * A reply the meeting heard has finished (Nova's END_TURN).
   *
   * @param lookupRunning A lookup is still in flight, so the reply that ended
   *   was its preamble and the answer is still to come.
   */
  noteAnswerHeard(lookupRunning: boolean = false): void {
    if (lookupRunning) return;
    this.owed = false;
    this.lookupPending = false;
    this.transcriptAddressedAt = 0;
  }

  /** Forget everything: the activation ended, or the mode changed. */
  reset(): void {
    this.owed = false;
    this.lastAddressedAt = 0;
    this.lookupPending = false;
    this.lastTurn = null;
    this.transcriptAddressedAt = 0;
  }
}
