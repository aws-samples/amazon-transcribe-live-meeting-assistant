/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const { subscribers, notifications, graphql } = vi.hoisted(() => ({
  subscribers: [],
  notifications: [],
  graphql: vi.fn(),
}));

// Both modules call generateClient() / ConsoleLogger at module load; stub
// aws-amplify so importing them stays side-effect free. The stubbed client
// answers the backfill query and hands back a subscription whose `next` the
// tests below drive directly, which is what lets the sequence of updates a
// live meeting would produce be replayed in order.
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({ ConsoleLogger: class {} }));

// eslint-disable-next-line import/first
import ManualActionAlerts, { isUndescribedPrompt, promptIdentity, shouldClearManualAlert } from './ManualActionAlerts';
// eslint-disable-next-line import/first
import { VP_STATUS_CONFIG } from './VirtualParticipantDetails';

describe('shouldClearManualAlert', () => {
  it('keeps the alert up only while the prompt is open', () => {
    expect(shouldClearManualAlert('MANUAL_ACTION_REQUIRED')).toBe(false);
  });

  it('clears the alert on every other status the VP can report', () => {
    // Including the pre-admission ones. A prompt raised during sign-in or
    // pre-join resumes CONNECTING or JOINING when it is cleared, so an alert
    // that only cleared on JOINED / ACTIVE / COMPLETED would stay on screen
    // until the VP joined — or for the whole meeting, if the prompt was the
    // reason it never did.
    const statuses = Object.keys(VP_STATUS_CONFIG).filter((s) => s !== 'MANUAL_ACTION_REQUIRED');
    expect(statuses).toContain('JOINING');
    expect(statuses).toContain('CONNECTING');
    statuses.forEach((status) => {
      expect(shouldClearManualAlert(status), status).toBe(true);
    });
  });

  it('treats an update carrying no status as saying nothing about the prompt', () => {
    expect(shouldClearManualAlert(undefined)).toBe(false);
    expect(shouldClearManualAlert(null)).toBe(false);
    expect(shouldClearManualAlert('')).toBe(false);
  });
});

describe('isUndescribedPrompt', () => {
  // A subscription payload carries the fields the mutation behind it selected.
  // An update written by a caller that did not select the prompt fields arrives
  // with the prompt status and nulls, which is not something to act on: the
  // real message would be replaced by generic text and the notification would
  // fire a second time.
  it('recognises a prompt status with no prompt to show', () => {
    expect(isUndescribedPrompt({ status: 'MANUAL_ACTION_REQUIRED' })).toBe(true);
    expect(isUndescribedPrompt({ status: 'MANUAL_ACTION_REQUIRED', manualActionType: null })).toBe(true);
  });

  it('accepts a prompt that describes itself', () => {
    expect(
      isUndescribedPrompt({
        status: 'MANUAL_ACTION_REQUIRED',
        manualActionType: 'LOGIN',
        manualActionMessage: 'Sign in via the live view',
      }),
    ).toBe(false);
  });

  it('says nothing about an update that is not reporting a prompt', () => {
    expect(isUndescribedPrompt({ status: 'ACTIVE' })).toBe(false);
    expect(isUndescribedPrompt({ status: 'FAILED', errorMessage: 'Wrong meeting password' })).toBe(false);
    expect(isUndescribedPrompt({})).toBe(false);
    expect(isUndescribedPrompt(null)).toBe(false);
  });
});

describe('promptIdentity', () => {
  it('is the same for two reports of one prompt', () => {
    const raised = { manualActionStartTime: '2026-10-22T10:00:00.000Z', manualActionType: 'CAPTCHA' };
    expect(promptIdentity({ ...raised, status: 'MANUAL_ACTION_REQUIRED' })).toBe(
      promptIdentity({ ...raised, status: 'MANUAL_ACTION_REQUIRED', CallId: 'call-1' }),
    );
  });

  it('differs for a prompt raised afresh, so a restarted timeout is shown again', () => {
    const first = { manualActionStartTime: '2026-10-22T10:00:00.000Z', manualActionType: 'CAPTCHA' };
    const again = { manualActionStartTime: '2026-10-22T10:06:00.000Z', manualActionType: 'CAPTCHA' };
    expect(promptIdentity(first)).not.toBe(promptIdentity(again));
  });

  it('differs for a different kind of prompt even without a start time', () => {
    expect(promptIdentity({ manualActionType: 'CAPTCHA' })).not.toBe(promptIdentity({ manualActionType: 'LOGIN' }));
  });
});

// ---------------------------------------------------------------------------
// The sequence of updates a live prompt actually produces
// ---------------------------------------------------------------------------

const PROMPT = {
  id: 'vp-1',
  meetingName: 'Weekly sync',
  meetingPlatform: 'TEAMS',
  status: 'MANUAL_ACTION_REQUIRED',
  manualActionType: 'CAPTCHA',
  manualActionMessage: 'Solve the CAPTCHA in the live view',
  manualActionTimeoutSeconds: 300,
  manualActionStartTime: '2026-10-22T10:00:00.000Z',
  updatedAt: '2026-10-22T10:00:00.000Z',
};

class FakeNotification {
  constructor(title, options) {
    notifications.push({ title, ...options });
  }

  // eslint-disable-next-line class-methods-use-this
  close() {}
}
FakeNotification.permission = 'granted';
FakeNotification.requestPermission = async () => 'granted';

/** Push one subscription payload through the mounted component. */
const emit = (vp) =>
  act(() => {
    subscribers.forEach((next) => next({ data: { onUpdateVirtualParticipant: vp } }));
  });

/** Let every pending notification debounce elapse. */
const flushNotifications = () =>
  act(() => {
    vi.advanceTimersByTime(20_000);
  });

describe('alerts across the updates one prompt produces', () => {
  beforeEach(async () => {
    subscribers.length = 0;
    notifications.length = 0;
    window.localStorage.clear();
    globalThis.Notification = FakeNotification;
    graphql.mockImplementation((operation) => {
      if (String(operation?.query).includes('subscription')) {
        return {
          subscribe: ({ next }) => {
            subscribers.push(next);
            return { unsubscribe: () => {} };
          },
        };
      }
      return Promise.resolve({ data: { listVirtualParticipants: [] } });
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await act(async () => {
      render(
        <MemoryRouter>
          <ManualActionAlerts />
        </MemoryRouter>,
      );
    });
    expect(subscribers.length).toBeGreaterThan(0);
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    delete globalThis.Notification;
  });

  it('notifies once for a prompt, through every update that follows it', () => {
    // Writes while a prompt is open are normal: a join sub-step from the join
    // loop that runs alongside the dialog watchdog, and the CallId link
    // mid-meeting. Each returns the open prompt along with whatever it changed,
    // so each arrives as a fully described prompt — and only comparing it with
    // what is already on screen keeps the user from being chimed at repeatedly
    // for a prompt they are already looking at.
    //
    // The debounce window is allowed to elapse between each update, which is
    // both the realistic timing — a prompt waits on a human for minutes while
    // these writes continue — and what makes this test able to fail: four
    // updates inside one window would collapse into a single notification
    // whether or not they were deduplicated.
    emit(PROMPT);
    flushNotifications();
    emit({ ...PROMPT, errorMessage: 'Waiting to be admitted…' }); // a join sub-step
    flushNotifications();
    emit({ ...PROMPT, CallId: 'call-1234' }); // the CallId link
    flushNotifications();
    emit(PROMPT); // the same prompt reported again
    flushNotifications();

    expect(notifications).toHaveLength(1);
    expect(notifications[0].title).toContain('Weekly sync');
    expect(notifications[0].body).toContain('Solve the CAPTCHA in the live view');
  });

  it('keeps showing the prompt through those updates, with its own wording', () => {
    emit(PROMPT);
    emit({ ...PROMPT, errorMessage: 'Waiting to be admitted…' });

    // Still the specific message, not the generic fallback, and one alert.
    expect(screen.getAllByText(/Solve the CAPTCHA in the live view/)).toHaveLength(1);
    expect(screen.queryByText(/waiting on you to complete a sign-in or CAPTCHA/)).toBeNull();
    expect(screen.getAllByText(/Action required: CAPTCHA — Weekly sync/)).toHaveLength(1);
  });

  it('notifies again for a second, different prompt', () => {
    emit(PROMPT);
    flushNotifications();
    expect(notifications).toHaveLength(1);

    emit({
      ...PROMPT,
      manualActionType: 'LOGIN',
      manualActionMessage: 'Finish signing in',
      manualActionStartTime: '2026-10-22T10:07:00.000Z',
    });
    flushNotifications();

    expect(notifications).toHaveLength(2);
    expect(notifications[1].body).toContain('Finish signing in');
  });

  it('notifies again for the same kind of prompt raised afresh', () => {
    // A prompt that timed out and was raised again carries a new start time,
    // which is a new prompt as far as the user is concerned.
    emit(PROMPT);
    flushNotifications();
    emit({ ...PROMPT, manualActionStartTime: '2026-10-22T10:06:00.000Z' });
    flushNotifications();

    expect(notifications).toHaveLength(2);
  });

  it('shows a prompt again after it was resolved, even reported identically', () => {
    // Clearing forgets which prompt was on screen, so the deduplication cannot
    // outlive the alert it was suppressing duplicates of. Replaying the very
    // same payload is the case that proves it: subscription delivery is not
    // ordered, so an update from before the prompt cleared can arrive after it,
    // and the row it describes does say a prompt is open.
    emit(PROMPT);
    flushNotifications();
    emit({ ...PROMPT, status: 'ACTIVE' }); // resolved: the VP carried on
    expect(screen.queryByText(/Solve the CAPTCHA in the live view/)).toBeNull();

    emit(PROMPT);
    flushNotifications();

    expect(notifications).toHaveLength(2);
    expect(screen.getAllByText(/Solve the CAPTCHA in the live view/)).toHaveLength(1);
  });

  it('does not notify for an update that cannot describe the prompt', () => {
    // The backstop: a writer that did not select the prompt fields. Such an
    // update neither raises nor clears.
    emit(PROMPT);
    flushNotifications();
    emit({ id: 'vp-1', status: 'MANUAL_ACTION_REQUIRED', updatedAt: '2026-10-22T10:02:00.000Z' });
    flushNotifications();

    expect(notifications).toHaveLength(1);
    expect(screen.getAllByText(/Solve the CAPTCHA in the live view/)).toHaveLength(1);
  });
});
