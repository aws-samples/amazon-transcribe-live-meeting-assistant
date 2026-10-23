/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Global, app-shell-level alerts for the user's Virtual Participants. Listens
 * to onUpdateVirtualParticipant and surfaces:
 *   - MANUAL_ACTION_REQUIRED  → sticky Flashbar + browser Notification, with
 *                               a one-click link to the VP's live VNC viewer.
 *   - FAILED                  → red Flashbar with errorMessage, dismissible.
 * Only the user's own VPs trigger alerts. Cleans up subscription on unmount.
 */
/* eslint-disable react/jsx-props-no-spreading */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { generateClient } from 'aws-amplify/api';
import { Flashbar } from '@cloudscape-design/components';

import { VIRTUAL_PARTICIPANT_PATH } from '../../routes/constants';

const client = generateClient();

/**
 * Whether an incoming VP update takes the sticky manual-action alert down.
 *
 * True for every status except MANUAL_ACTION_REQUIRED itself: a prompt that
 * has been dealt with returns the VP to whatever it was doing when the prompt
 * arrived, which is as often a pre-admission status as a mid-meeting one. An
 * update with no status at all is not evidence either way.
 */
export const shouldClearManualAlert = (status) => !!status && status !== 'MANUAL_ACTION_REQUIRED';

/**
 * Whether an update says a prompt is open without saying anything about it.
 *
 * A subscription payload carries the fields the mutation selected; one that did
 * not select the prompt fields reports the status and nulls. Such an update
 * cannot be acted on — there is nothing to show, and nothing to conclude from
 * its absence either. Only a prompting status reaches this, and the Virtual
 * Participant container is the only thing that writes one; it selects the
 * prompt fields, so this is a backstop against a writer that does not.
 *
 * Separate from the deduplication below, which covers the opposite case: a
 * fully described payload for a prompt that is already on screen. The two guard
 * different failures and removing either reopens one of them.
 */
export const isUndescribedPrompt = (vp) => vp?.status === 'MANUAL_ACTION_REQUIRED' && !vp?.manualActionType;

/**
 * Identity of the prompt an update is reporting, for telling a new prompt from
 * another report of the one already on screen.
 *
 * Any write to the VP record while a prompt is open returns the prompt fields
 * along with everything else, so the alert sees a stream of identical prompts —
 * a join sub-step, a CallId link. Rebuilding on each would re-render an alert
 * the user is already reading and sound the chime again.
 *
 * `manualActionStartTime` is the identity because it is stamped once, when the
 * prompt is raised: a second, different prompt and a prompt whose timeout has
 * been restarted each carry a new one and so count as new, while a repeat of
 * the same prompt does not. `manualActionType` is folded in so that an update
 * somehow lacking the timestamp still tells one kind of prompt from another.
 */
export const promptIdentity = (vp) => `${vp?.manualActionStartTime || ''}#${vp?.manualActionType || ''}`;

const ON_UPDATE_VP = /* GraphQL */ `
  subscription OnUpdateVirtualParticipant {
    onUpdateVirtualParticipant {
      id
      meetingName
      meetingPlatform
      status
      Owner
      SharedWith
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
      errorMessage
      updatedAt
    }
  }
`;

// Backfill query: when the user opens the LMA UI for the first time,
// AppSync subscriptions only deliver events from that moment forward.
// We query for any active VPs the user owns so a manual-action that
// happened seconds before page load is still surfaced.
const LIST_VPS = /* GraphQL */ `
  query ListVirtualParticipants {
    listVirtualParticipants {
      id
      meetingName
      meetingPlatform
      status
      Owner
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
      errorMessage
      updatedAt
    }
  }
`;

const ALERT_KIND = { MANUAL: 'MANUAL', FAILED: 'FAILED' };

// localStorage-backed dismissal so banners stay closed across page
// refreshes and route changes. Keyed by alertId (e.g. "<vpId>#failed").
// We cap the size to avoid unbounded growth on long-lived sessions.
const LS_KEY = 'lma.manualActionAlerts.dismissed.v1';
const LS_CAP = 200;

const loadDismissed = () => {
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
};
const saveDismissed = (set) => {
  try {
    const arr = Array.from(set);
    // Drop oldest if over cap (Set preserves insertion order).
    const trimmed = arr.length > LS_CAP ? arr.slice(arr.length - LS_CAP) : arr;
    window.localStorage.setItem(LS_KEY, JSON.stringify(trimmed));
  } catch {
    // ignore — quota / private mode etc.
  }
};

// localStorage flag for the "Enable desktop notifications" Flashbar — once
// the user dismisses it, don't show it again on this device. (Distinct
// from the per-VP dismissals above; this is a single global flag.)
const PERM_PROMPT_DISMISSED_KEY = 'lma.manualActionAlerts.permPromptDismissed.v1';
const isPermPromptDismissed = () => {
  try {
    return window.localStorage.getItem(PERM_PROMPT_DISMISSED_KEY) === '1';
  } catch {
    return false;
  }
};
const markPermPromptDismissed = () => {
  try {
    window.localStorage.setItem(PERM_PROMPT_DISMISSED_KEY, '1');
  } catch {
    /* ignore */
  }
};

// 15-second debounce on the desktop-Notification + chime path: many MANUAL
// transitions auto-clear within seconds (e.g. an unknown consent dialog the
// VP detects then dismisses). Firing a desktop notification immediately on
// every transition would be noisy. The Flashbar still appears immediately;
// only the OS-level notification + chime is debounced.
const NOTIFICATION_DEBOUNCE_MS = 15_000;

// Tiny WebAudio-generated chime so we don't ship a separate audio asset.
// Two short beeps (660Hz then 880Hz) at low volume — distinctive but unobtrusive.
// Browsers gate AudioContext on a user-gesture; if the page hasn't received
// one yet, ctx.state will be 'suspended' and we try ctx.resume() (no-op
// failure if it can't). Notification.requestPermission() counts as a
// gesture, so once the user has granted notifications, the chime works.
const playAlertChime = async () => {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    if (ctx.state === 'suspended') {
      try {
        await ctx.resume();
      } catch {
        /* still suspended — beep won't be audible but Notification visual still shows */
      }
    }
    const now = ctx.currentTime;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.18, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.55);
    gain.connect(ctx.destination);
    [
      [660, now + 0.0],
      [880, now + 0.18],
    ].forEach(([freq, t]) => {
      const osc = ctx.createOscillator();
      osc.type = 'sine';
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(t);
      osc.stop(t + 0.16);
    });
    setTimeout(() => ctx.close().catch(() => {}), 800);
  } catch {
    // AudioContext can fail when the document hasn't received a user gesture
    // yet (browser autoplay policy). Notifications still surface visually.
  }
};

const ManualActionAlerts = () => {
  const navigate = useNavigate();
  const [alerts, setAlerts] = useState([]);
  const dismissedRef = useRef(loadDismissed());
  // Track Notification.permission in state so the "enable notifications"
  // banner re-renders after the user grants/denies. Initial value is read
  // synchronously so the banner appears on first paint when needed.
  const [notificationPermission, setNotificationPermission] = useState(() =>
    typeof Notification !== 'undefined' ? Notification.permission : 'denied',
  );
  const [permPromptDismissed, setPermPromptDismissed] = useState(() => isPermPromptDismissed());
  // Pending desktop-notification timers keyed by `${vpId}#${kind}` — lets us
  // cancel (suppress) a notification if the VP transitions out of
  // MANUAL_ACTION_REQUIRED within the debounce window.
  const pendingNotificationsRef = useRef(new Map());
  // The prompt currently on screen for each VP, by `promptIdentity`. Held in a
  // ref rather than derived from `alerts` because the handler is a useCallback
  // that would otherwise close over a stale copy of that state. Dropped when
  // the prompt clears, so a later prompt for the same VP is shown afresh.
  const shownPromptRef = useRef(new Map());

  // Triggered by the click on the "Enable desktop notifications" banner
  // button. Browsers require a real user gesture for requestPermission()
  // to actually show the OS-level prompt — calling it from useEffect on
  // mount silently no-ops in Chrome 80+, all Safari, and all Firefox.
  const requestNotificationPermission = useCallback(async () => {
    if (typeof Notification === 'undefined') return;
    try {
      const result = await Notification.requestPermission();
      setNotificationPermission(result);
      // Side effect: requesting permission is also a user gesture for the
      // AudioContext, so playAlertChime() will work going forward.
    } catch {
      // Some browsers throw if called from a non-secure context; ignore.
    }
  }, []);

  const dismiss = useCallback((id) => {
    dismissedRef.current.add(id);
    saveDismissed(dismissedRef.current);
    setAlerts((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const openVPViewer = useCallback(
    (vpId) => {
      navigate(`${VIRTUAL_PARTICIPANT_PATH}/${vpId}`);
    },
    [navigate],
  );

  const cancelPendingNotification = useCallback((vpId, kind) => {
    const key = `${vpId}#${kind}`;
    const handle = pendingNotificationsRef.current.get(key);
    if (handle) {
      clearTimeout(handle);
      pendingNotificationsRef.current.delete(key);
    }
  }, []);

  const fireBrowserNotification = useCallback(
    (vp, kind) => {
      if (typeof Notification === 'undefined') return;
      if (Notification.permission !== 'granted') return;
      const key = `${vp.id}#${kind}`;
      // Schedule with debounce; if a later VP update cancels it, we suppress.
      cancelPendingNotification(vp.id, kind);
      const handle = setTimeout(() => {
        pendingNotificationsRef.current.delete(key);
        const titlePrefix = kind === ALERT_KIND.MANUAL ? 'LMA needs your help' : 'LMA virtual participant failed';
        const body =
          kind === ALERT_KIND.MANUAL
            ? `${vp.manualActionType || 'Manual action'} required: ${vp.manualActionMessage || 'Open the LMA viewer'}`
            : vp.errorMessage || 'See LMA for details.';
        try {
          const note = new Notification(`${titlePrefix} — ${vp.meetingName || vp.meetingPlatform}`, {
            body,
            tag: `lma-vp-${vp.id}-${kind}`, // collapses duplicates per VP+kind
            requireInteraction: kind === ALERT_KIND.MANUAL,
          });
          note.onclick = () => {
            window.focus();
            openVPViewer(vp.id);
            note.close();
          };
          playAlertChime();
        } catch {
          // Notification API rarely throws; ignore.
        }
      }, NOTIFICATION_DEBOUNCE_MS);
      pendingNotificationsRef.current.set(key, handle);
    },
    [cancelPendingNotification, openVPViewer],
  );

  useEffect(() => {
    let cancelled = false;
    // One-time backfill: surface any active manual-action / failed VP that
    // happened before the subscription was live. listVirtualParticipants
    // already returns only the user's own VPs (AppSync auth resolver
    // filters server-side), so no client-side owner filter is needed.
    (async () => {
      try {
        const r = await client.graphql({ query: LIST_VPS });
        if (cancelled) return;
        const vps = r?.data?.listVirtualParticipants || [];
        const fresh = vps.filter((vp) => {
          if (!vp) return false;
          if (vp.status === 'MANUAL_ACTION_REQUIRED') return true;
          if (vp.status === 'FAILED' && vp.updatedAt) {
            const ageMs = Date.now() - new Date(vp.updatedAt).getTime();
            if (ageMs >= 0 && ageMs < 30 * 60_000) return true;
          }
          return false;
        });
        // eslint-disable-next-line no-console
        console.debug(`[ManualActionAlerts] backfill: ${vps.length} VPs total, ${fresh.length} need an alert`);
        // eslint-disable-next-line no-use-before-define
        fresh.forEach((vp) => handleVPUpdate(vp));
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn('[ManualActionAlerts] backfill failed:', e);
      }
    })();
    return () => {
      cancelled = true;
    };
    // handleVPUpdate is stable (useCallback below).
  }, []);

  // NOTE: we intentionally do NOT call Notification.requestPermission()
  // from a useEffect on mount. Modern browsers require a real user gesture
  // (click, keypress) for the OS-level permission prompt to render —
  // calling it from useEffect either silently no-ops or returns 'default'
  // immediately. Instead, the "Enable desktop notifications" Flashbar
  // below offers an explicit button that wraps requestPermission() in a
  // user-gesture handler.

  const handleVPUpdate = useCallback(
    (vp) => {
      if (!vp) return;
      // No client-side owner filter. AppSync subscriptions on the
      // VirtualParticipant type are authorized via @aws_cognito_user_pools
      // — the user's own subscription connection only delivers events for
      // VPs they're allowed to see. Adding a client-side Owner-equality
      // check on top was creating a race (events arriving before
      // fetchUserAttributes resolved would all be dropped) and was
      // suppressing legitimate alerts.
      // eslint-disable-next-line no-console
      console.debug('[ManualActionAlerts] received VP update', {
        id: vp.id,
        status: vp.status,
        owner: vp.Owner,
      });
      // A subscription delivers the fields the mutation behind it selected, so
      // an update from a writer that did not select the prompt fields arrives
      // with the prompt status and nothing to describe it. That payload is not
      // evidence about the prompt in either direction: rebuilding the alert
      // from it would replace the real message with generic text, and clearing
      // it would drop an alert the VP still needs. The container selects those
      // fields on every write, so this is the backstop for a writer that
      // does not.
      if (isUndescribedPrompt(vp)) {
        return;
      }
      // The sticky MANUAL alert belongs to a prompt that is still open, so it
      // goes as soon as the VP reports any other status — not only the three
      // that mean it is in the meeting. A prompt raised before admission
      // resumes JOINING or CONNECTING when it is cleared, and the alert has to
      // go then too; the viewer's own banner, which keys off the status
      // directly, already does. A payload carrying no status says nothing
      // about the prompt and clears nothing.
      //
      // This also cancels a desktop notification still inside the 15s debounce
      // window: most prompts clear quickly (a transient consent dialog the VP
      // dismisses itself), and an OS notification for one of those is noise.
      if (shouldClearManualAlert(vp.status)) {
        setAlerts((prev) => prev.filter((a) => a.id !== `${vp.id}#manual`));
        cancelPendingNotification(vp.id, ALERT_KIND.MANUAL);
        // Forget which prompt was on screen, so the deduplication below cannot
        // outlive the alert whose duplicates it was suppressing.
        shownPromptRef.current.delete(vp.id);
      }

      if (vp.status === 'MANUAL_ACTION_REQUIRED') {
        const alertId = `${vp.id}#manual`;
        if (dismissedRef.current.has(alertId)) return;
        // Only a prompt that is not already on screen is worth showing again.
        // Every write to the VP record returns the open prompt along with
        // whatever it changed, and the writes that land during a prompt are
        // ones this component should not react to — a join sub-step from the
        // join loop that runs alongside the dialog watchdog, a CallId link
        // mid-meeting. Rebuilding on those re-renders an alert the user is
        // reading and sounds the chime again. A genuinely new prompt, or the
        // same kind raised afresh with its timeout restarted, carries a new
        // start time and so passes.
        const identity = promptIdentity(vp);
        if (shownPromptRef.current.get(vp.id) === identity) return;
        shownPromptRef.current.set(vp.id, identity);
        setAlerts((prev) => [
          ...prev.filter((a) => a.id !== alertId),
          {
            id: alertId,
            type: 'warning',
            dismissible: true,
            onDismiss: () => dismiss(alertId),
            header: `Action required: ${vp.manualActionType || 'manual'} — ${vp.meetingName || vp.meetingPlatform}`,
            content:
              vp.manualActionMessage ||
              'LMA is waiting on you to complete a sign-in or CAPTCHA in the meeting browser.',
            buttonText: 'Open viewer',
            onButtonClick: () => openVPViewer(vp.id),
          },
        ]);
        fireBrowserNotification(vp, ALERT_KIND.MANUAL);
      } else if (vp.status === 'FAILED') {
        const alertId = `${vp.id}#failed`;
        if (dismissedRef.current.has(alertId)) return;
        setAlerts((prev) => [
          ...prev.filter((a) => a.id !== alertId),
          {
            id: alertId,
            type: 'error',
            dismissible: true,
            onDismiss: () => dismiss(alertId),
            header: `Virtual participant failed — ${vp.meetingName || vp.meetingPlatform}`,
            content: vp.errorMessage || 'See the Virtual Participants page for details.',
            buttonText: 'Open',
            onButtonClick: () => openVPViewer(vp.id),
          },
        ]);
        fireBrowserNotification(vp, ALERT_KIND.FAILED);
      }
    },
    [cancelPendingNotification, dismiss, fireBrowserNotification, openVPViewer],
  );

  useEffect(() => {
    let subscription;
    try {
      subscription = client.graphql({ query: ON_UPDATE_VP }).subscribe({
        next: (msg) => handleVPUpdate(msg?.data?.onUpdateVirtualParticipant),
        error: () => {
          // Subscription errors are common during cold-start; let it retry.
        },
      });
    } catch {
      // No-op — if subscription fails to initialize, the user just won't get
      // proactive alerts; they'll still see status on the VP page.
    }
    return () => {
      try {
        subscription?.unsubscribe?.();
      } catch {
        // ignore
      }
    };
  }, [handleVPUpdate]);

  if (!alerts.length) return null;
  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        zIndex: 5000,
      }}
    >
      <Flashbar
        items={[
          // Synthetic "enable desktop notifications" prompt at the top.
          // Shown only when the browser supports Notifications, permission
          // hasn't been granted *or* denied (default = "not asked yet"),
          // and the user hasn't dismissed it on this device.
          ...(typeof Notification !== 'undefined' && notificationPermission === 'default' && !permPromptDismissed
            ? [
                {
                  id: 'lma-vp-notif-permission',
                  type: 'info',
                  dismissible: true,
                  onDismiss: () => {
                    markPermPromptDismissed();
                    setPermPromptDismissed(true);
                  },
                  header: 'Enable desktop notifications for VP alerts',
                  content:
                    'When a Virtual Participant needs your help (CAPTCHA, 2FA, sign-in challenge), ' +
                    "LMA can show a desktop notification + audio chime so you don't miss it while in another tab.",
                  buttonText: 'Enable notifications',
                  onButtonClick: requestNotificationPermission,
                },
              ]
            : []),
          ...alerts,
        ]}
        stackItems
      />
    </div>
  );
};

export default ManualActionAlerts;
