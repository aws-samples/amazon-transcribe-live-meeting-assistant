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
 *   turn that contains the wake phrase;
 * - a turn that follows an addressed one within ADDRESSED_FOLLOW_ON_MS, which
 *   is the same request split by a pause ("Hey Alex ... what's on the agenda");
 * - any turn while the answer to a lookup an addressed request started is
 *   still to come. The participants go on talking while they wait, and on the
 *   live meeting the model delivered the lookup's answer as its reply to one
 *   of those turns, so withholding it would have withheld the answer.
 *
 * Every other turn withholds the reply to it: its audio is dropped and its text
 * kept out of the transcript and the history, the same treatment an unaddressed
 * `set_mode` reply already gets. A withheld reply plays nothing, so it does not
 * hold the activation window open either, and the window lapses on its own
 * idle timeout.
 *
 * Once an answer has been heard in full, nothing more is owed: the next reply
 * needs the next addressed turn. A participant who talks over the assistant
 * without addressing it ends the answer the same way, because the reply the
 * model produces next is to the interruption.
 */

/** How long after an addressed turn a following turn still counts as part of
 *  the same request. Nova Sonic's turn detection splits an utterance at a
 *  pause of well under a second; a participant who has finished asking and
 *  starts talking to the room usually leaves longer than this. */
export const ADDRESSED_FOLLOW_ON_MS = 4000;

/** Why a turn's reply is, or is not, let through. */
export type ReplyGateReason = 'addressed' | 'follow-on' | 'lookup-pending' | 'not-addressed';

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

  constructor(private readonly followOnMs: number = ADDRESSED_FOLLOW_ON_MS) {}

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
    if (addressed) {
      this.owed = true;
      this.lastAddressedAt = now;
      return { audible: true, reason: 'addressed' };
    }
    if (this.lastAddressedAt > 0 && now - this.lastAddressedAt <= this.followOnMs) {
      this.owed = true;
      this.lastAddressedAt = now;
      return { audible: true, reason: 'follow-on' };
    }
    if (this.lookupPending) {
      return { audible: true, reason: 'lookup-pending' };
    }
    this.owed = false;
    return { audible: false, reason: 'not-addressed' };
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
  }

  /** Forget everything: the activation ended, or the mode changed. */
  reset(): void {
    this.owed = false;
    this.lastAddressedAt = 0;
    this.lookupPending = false;
  }
}
