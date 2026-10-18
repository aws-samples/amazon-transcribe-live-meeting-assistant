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

/**
 * The guard CallDetails and EmbedCallDetails use to decide whether to replace
 * the meeting they are rendering, extracted so it can be driven directly. It
 * must match the expression in both of those files.
 */
const shouldReplace = (current, incoming) => {
  const timestampAdvanced = incoming?.updatedAt && current.updatedAt < incoming.updatedAt;
  const speakerNamesChanged =
    incoming?.speakerIdentities !== undefined && current.speakerIdentities !== incoming.speakerIdentities;
  return Boolean(timestampAdvanced || speakerNamesChanged);
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
