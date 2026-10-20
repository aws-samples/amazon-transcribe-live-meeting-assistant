/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The glue that a rename has to travel through to reach the screen.
 *
 * Two defects hid here behind a fully green unit suite, because nothing
 * exercised the seam between the new identity map and existing UI behaviour:
 *
 *  1. CallDetails only re-rendered the meeting when `UpdatedAt` advanced, and a
 *     rename deliberately leaves `UpdatedAt` alone -- so a rename did nothing
 *     until the page was reloaded.
 *  2. The mutation selected only the two fields it wrote. That response is what
 *     AppSync publishes to `onUpdateCall` subscribers, so every other field
 *     arrived null and the subscription handler wrote those nulls over the
 *     meeting in the list.
 *
 * These tests pin both rules against the real helpers rather than mocking them.
 */
import { describe, it, expect } from 'vitest';

import updateSpeakerIdentitiesMutation from '../../graphql/queries/updateSpeakerIdentities';
import updateCallSummaryTextMutation from '../../graphql/queries/updateCallSummaryText';
import onUpdateCallSubscription from '../../graphql/queries/onUpdateCall';
import mapCallsAttributes from '../common/map-call-attributes';

/** Field names selected by a GraphQL document, flattened.
 *
 * Drops the operation signature (`subscription Subscription(...)`) and any
 * variable type names, which are not selected fields.
 */
const NOT_A_FIELD = new Set(['Subscription', 'Mutation', 'Query']);
const selected = (doc) =>
  new Set(
    doc
      .replace(/[{}()$:!]/g, ' ')
      .split(/\s+/)
      .filter((t) => /^[A-Z][A-Za-z]*$/.test(t))
      .filter((t) => !NOT_A_FIELD.has(t) && !t.endsWith('Input')),
  );

describe('the rename mutation feeds the onUpdateCall broadcast', () => {
  it('selects every field the subscription does', () => {
    // AppSync publishes the MUTATION's response to subscribers. A field the
    // subscription asks for that the mutation did not select arrives as null,
    // and the handler writes that null over the stored meeting -- blanking its
    // status, start time and summary in every connected client's list.
    const missing = [...selected(onUpdateCallSubscription)].filter(
      (f) => !selected(updateSpeakerIdentitiesMutation).has(f),
    );
    expect(missing).toEqual([]);
  });

  it('selects the field it actually writes', () => {
    expect(selected(updateSpeakerIdentitiesMutation).has('SpeakerIdentities')).toBe(true);
  });
});

describe('the summary mutation feeds the onUpdateCall broadcast', () => {
  // Same rule, same reason: this mutation is in onUpdateCall's @aws_subscribe
  // list too, so a narrow selection set would blank the rest of the meeting in
  // every connected client's list.
  it('selects every field the subscription does', () => {
    const missing = [...selected(onUpdateCallSubscription)].filter(
      (f) => !selected(updateCallSummaryTextMutation).has(f),
    );
    expect(missing).toEqual([]);
  });

  it('selects the fields it actually writes', () => {
    expect(selected(updateCallSummaryTextMutation).has('CallSummaryText')).toBe(true);
    expect(selected(updateCallSummaryTextMutation).has('CallSummaryState')).toBe(true);
  });

  it('is a field the subscription carries, so a new summary reaches open pages', () => {
    expect(selected(onUpdateCallSubscription).has('CallSummaryState')).toBe(true);
  });
});

/**
 * The guard the three views that render CallPanel use to decide whether to
 * replace the meeting they are showing, extracted so it can be driven directly.
 * It must match the expression in all of CallDetails.js, EmbedCallDetails.jsx
 * and EmbedVirtualParticipant.jsx.
 */
const shouldReplace = (current, incoming) => {
  const timestampAdvanced = incoming?.updatedAt && current.updatedAt < incoming.updatedAt;
  const speakerNamesChanged =
    incoming?.speakerIdentities !== undefined && current.speakerIdentities !== incoming.speakerIdentities;
  const summaryChanged =
    (incoming?.callSummaryText !== undefined && current.callSummaryText !== incoming.callSummaryText) ||
    (incoming?.callSummaryState !== undefined && current.callSummaryState !== incoming.callSummaryState);
  return Boolean(timestampAdvanced || speakerNamesChanged || summaryChanged);
};

describe('the meeting-refresh guard', () => {
  const NAMED = '{"identities":{"CALLER#Other Participant (spk_0)":{"name":"Bob"}}}';
  const loaded = { updatedAt: '2026-10-17T12:00:00.000Z', speakerIdentities: NAMED };

  it('replaces the meeting when the names changed but the timestamp did not', () => {
    // A rename deliberately does not move UpdatedAt, so this is the only signal.
    const renamed = { updatedAt: loaded.updatedAt, speakerIdentities: `${NAMED} ` };
    expect(shouldReplace(loaded, renamed)).toBe(true);
  });

  it('does NOT replace it from a meeting-list copy that carries no names', () => {
    // listCallsDateRange omits SpeakerIdentities, and a list refresh empties the
    // calls array before repopulating it, so the list-derived copy of an open
    // meeting routinely has undefined here. Treating that as "the names were
    // cleared" blanks every name on screen until the page is reloaded.
    const fromList = { updatedAt: loaded.updatedAt, speakerIdentities: undefined };
    expect(shouldReplace(loaded, fromList)).toBe(false);
  });

  it('still replaces it when the timestamp advances, names or no names', () => {
    const later = { updatedAt: '2026-10-17T12:30:00.000Z', speakerIdentities: undefined };
    expect(shouldReplace(loaded, later)).toBe(true);
  });

  it('replaces it when the names are genuinely cleared', () => {
    // Clearing every name stores an empty map, not undefined, so it is a change.
    const cleared = { updatedAt: loaded.updatedAt, speakerIdentities: '{"identities":{}}' };
    expect(shouldReplace(loaded, cleared)).toBe(true);
  });

  it('does not replace it when nothing changed', () => {
    expect(shouldReplace(loaded, { ...loaded })).toBe(false);
  });

  it('replaces the meeting when a regenerated summary arrives', () => {
    // Editing and regenerating leave UpdatedAt alone for the same reason a
    // rename does, so this is the only signal that the summary moved.
    const summarised = { ...loaded, callSummaryText: 'A new summary.' };
    expect(shouldReplace({ ...loaded, callSummaryText: 'The old summary.' }, summarised)).toBe(true);
  });

  it('replaces the meeting when only the summary state changed', () => {
    // How the panel learns a run has started, or failed, in another tab.
    const running = { ...loaded, callSummaryState: '{"status":"running"}' };
    expect(shouldReplace({ ...loaded, callSummaryState: '{"status":"idle"}' }, running)).toBe(true);
  });

  it('does NOT replace it from a copy that carries no summary information', () => {
    const fromList = { updatedAt: loaded.updatedAt, speakerIdentities: undefined };
    expect(shouldReplace({ ...loaded, callSummaryText: 'A summary.' }, fromList)).toBe(false);
  });
});

/**
 * The guard above is a copy. These assert that the three views it stands in for
 * really do carry each clause, since a copy that silently drifts from the code
 * is worse than no test at all.
 */
/** A sibling source file, read as text. */
const read = async (relativePath) => {
  const { readFileSync } = await import('fs');
  const { fileURLToPath } = await import('url');
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf-8');
};

describe('every view that renders CallPanel uses the same refresh guard', () => {
  const views = [
    '../call-details/CallDetails.js',
    '../embed/EmbedCallDetails.jsx',
    '../embed/EmbedVirtualParticipant.jsx',
  ];

  it.each(views)('%s tests the timestamp, the names and the summary', async (view) => {
    const source = await read(view);
    expect(source).toContain('const timestampAdvanced =');
    expect(source).toContain('const speakerNamesChanged =');
    expect(source).toContain('const summaryChanged =');
    expect(source).toContain('timestampAdvanced || speakerNamesChanged || summaryChanged');
  });
});

/**
 * The calls context is built by three separate components, each enumerating its
 * fields by hand -- once where the hook's return value is destructured and again
 * where the context object is assembled. A writer added to the hook but not to
 * all six places is `undefined` at the point of use, so the control it drives
 * renders and then throws on the click. Nothing else notices: the components
 * that consume it are tested with their callbacks injected.
 */
describe('every provider of the calls context supplies the summary writers', () => {
  const providers = [
    '../call-analytics-layout/CallAnalyticsLayout.jsx',
    '../embed/EmbedCallDetails.jsx',
    '../embed/EmbedVirtualParticipant.jsx',
  ];

  it.each(providers)('%s destructures and re-exports both', async (provider) => {
    const source = await read(provider);
    for (const name of ['sendUpdateCallSummary', 'sendRegenerateCallSummary', 'sendUpdateSpeakerIdentities']) {
      // Twice: once out of the hook, once into the context value.
      expect(source.split(`${name},`).length - 1).toBeGreaterThanOrEqual(2);
    }
  });
});

describe('a renamed meeting still maps to a usable row', () => {
  it('throws nothing and keeps status and start time when only the names changed', () => {
    // The regression this guards: a payload missing Status/CreatedAt maps to an
    // "Unknown" status and a 1970 start time, which sorts the meeting to the
    // bottom of the list.
    const call = {
      CallId: 'c1',
      Status: 'ENDED',
      CreatedAt: '2026-10-17T12:00:00.000Z',
      UpdatedAt: '2026-10-17T12:30:00.000Z',
      SpeakerIdentities: '{"identities":{"CALLER#Other Participant (spk_0)":{"name":"Bob"}}}',
      TotalConversationDurationMillis: 1000,
    };
    const [mapped] = mapCallsAttributes([call], {});
    expect(mapped.speakerIdentities).toBe(call.SpeakerIdentities);
    expect(mapped.initiationTimeStamp).toBe('2026-10-17T12:00:00.000Z');
    expect(mapped.recordingStatusLabel).not.toBe('Unknown');
  });

  it('exposes speakerIdentities so the refresh guard can compare it', () => {
    // CallDetails re-renders when UpdatedAt advances OR this string changes; a
    // rename only moves the latter.
    const base = { CallId: 'c1', CreatedAt: '2026-10-17T12:00:00.000Z' };
    const [before] = mapCallsAttributes([{ ...base, SpeakerIdentities: '{"identities":{}}' }], {});
    const [after] = mapCallsAttributes([{ ...base, SpeakerIdentities: '{"identities":{"a":{"name":"B"}}}' }], {});
    expect(before.speakerIdentities).not.toBe(after.speakerIdentities);
  });
});
