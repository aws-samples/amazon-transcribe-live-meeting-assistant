/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Transcript Summary page loads and what it stores.
 *
 * The stored profile is a delta over the prompts the release ships, and these
 * two functions are the whole of that. Both failure modes are silent:
 *
 * - **Storing a section unchanged pins it.** The stored copy wins over the
 *   shipped one, so a later release that improves that prompt would never
 *   reach this deployment, and nothing in the UI would say so. Saving the page
 *   without editing anything must therefore store nothing.
 * - **Loading from either record alone loses sections.** With a delta profile,
 *   the editor filled from the profile would show only the edited sections, and
 *   the editor filled from the baseline would discard the edits. It is filled
 *   from the merge.
 *
 * Removal is the case that needs the explicit marker: a shipped section left
 * out of the profile is supplied by the release again, so a removal is stored
 * as `NONE`, which is what the summarizer skips.
 */
import { describe, it, expect } from 'vitest';

import { buildCustomTemplateConfig, isConflict, mergeTemplateConfig, promptsOnly } from './TranscriptSummaryPage';

const SHIPPED = {
  '1#Summary': 'shipped summary prompt',
  '2#Details': 'shipped details prompt',
  '3#Actions': 'shipped actions prompt',
};

// A read also returns the record's version and who saved it, in the same
// payload as the prompts.
const PROVENANCE = { Version: 4, UpdatedBy: 'admin@example.com', UpdatedAt: '2026-01-01T00:00:00Z' };

const asTemplates = (config) =>
  Object.entries(config)
    .map(([key, prompt]) => ({
      key,
      number: parseInt(key.split('#')[0], 10),
      label: key.split('#').slice(1).join('#'),
      prompt,
    }))
    .sort((a, b) => a.number - b.number);

describe('what the editor is filled from', () => {
  it('shows the shipped prompts when nothing has been customized', () => {
    expect(mergeTemplateConfig(SHIPPED, { ...PROVENANCE })).toEqual(SHIPPED);
  });

  it('shows an edited section alongside the shipped ones it does not replace', () => {
    expect(mergeTemplateConfig(SHIPPED, { ...PROVENANCE, '2#Details': 'my details prompt' })).toEqual({
      ...SHIPPED,
      '2#Details': 'my details prompt',
    });
  });

  it('leaves out a section that was turned off', () => {
    // NONE and empty are both how a section is suppressed, and the summarizer
    // skips both, so the page must not offer either back as if it were live.
    expect(mergeTemplateConfig(SHIPPED, { ...PROVENANCE, '2#Details': 'NONE', '3#Actions': '' })).toEqual({
      '1#Summary': 'shipped summary prompt',
    });
  });

  it('keeps a section the profile adds that the release does not ship', () => {
    expect(mergeTemplateConfig(SHIPPED, { '4#Risks': 'list the risks' })['4#Risks']).toBe('list the risks');
  });

  it('keeps the record version and the name of whoever saved it out of the editor', () => {
    expect(Object.keys(mergeTemplateConfig(SHIPPED, PROVENANCE)).sort()).toEqual(Object.keys(SHIPPED).sort());
  });

  it('survives an absent record on either side', () => {
    expect(mergeTemplateConfig(null, null)).toEqual({});
    expect(mergeTemplateConfig(undefined, SHIPPED)).toEqual(SHIPPED);
  });
});

describe('what the read-only panel of shipped prompts shows', () => {
  it('shows the prompts and nothing else', () => {
    // The read returns the record's version and who saved it in the same
    // payload as the prompts, and the panel prints what it is given, so an
    // unfiltered panel lists `Version` and `UpdatedBy` as if they were
    // templates.
    expect(promptsOnly({ ...SHIPPED, ...PROVENANCE })).toEqual(SHIPPED);
  });

  it('survives an absent record', () => {
    expect(promptsOnly(null)).toEqual({});
  });
});

describe('what a save stores', () => {
  it('stores nothing when the page was not edited', () => {
    // The whole point: an unchanged section keeps coming from the release, so a
    // later release that improves it still reaches this deployment.
    expect(buildCustomTemplateConfig(asTemplates(SHIPPED), SHIPPED)).toEqual({});
  });

  it('stores only the section that was edited', () => {
    const templates = asTemplates(SHIPPED);
    templates[1] = { ...templates[1], prompt: 'my details prompt' };
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({
      '2#Details': 'my details prompt',
    });
  });

  it('stores a renamed section under its new key and turns the old one off', () => {
    // The key carries the label, so renaming a section leaves the shipped one
    // behind; without the marker the release would supply it again and the
    // summary would hold both.
    const templates = asTemplates(SHIPPED);
    templates[0] = { ...templates[0], label: 'Overview' };
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({
      '1#Overview': 'shipped summary prompt',
      '1#Summary': 'NONE',
    });
  });

  it('turns off a shipped section that was deleted and nothing else', () => {
    // The one that matters: numbering by position on the page would renumber
    // every section after the deleted one, and none of those new keys would
    // match the shipped record -- so their unchanged text would be stored as a
    // pinned copy and a later release would never improve them again. A
    // section keeps its own number, so a deletion stores exactly one thing.
    const templates = asTemplates(SHIPPED).filter((entry) => entry.label !== 'Details');
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({ '2#Details': 'NONE' });
  });

  it('stores nothing for the sections after an added one', () => {
    const templates = [
      ...asTemplates(SHIPPED),
      { key: '4#Risks', number: 4, label: 'Risks', prompt: 'list the risks' },
    ];
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({
      '4#Risks': 'list the risks',
    });
  });

  it('stores a section the release does not ship', () => {
    const templates = [
      ...asTemplates(SHIPPED),
      { key: '4#Risks', number: 4, label: 'Risks', prompt: 'list the risks' },
    ];
    expect(buildCustomTemplateConfig(templates, SHIPPED)['4#Risks']).toBe('list the risks');
  });

  it('does not renumber the sections by their order in the list', () => {
    // The page offers no way to move a section, and the numbers travel with the
    // sections rather than with their positions, so a list in a different order
    // is still the same configuration and stores nothing.
    const reversed = asTemplates(SHIPPED).reverse();
    expect(buildCustomTemplateConfig(reversed, SHIPPED)).toEqual({});
  });

  it('gives a section carrying no number the next free one, not its position', () => {
    // Everything the page adds carries a number, so this is the defensive path.
    // Numbering by position could repeat a number a loaded section already
    // holds, and the order of the two would then depend on how the tie breaks.
    const templates = [
      ...asTemplates(SHIPPED),
      { label: 'Risks', prompt: 'list the risks' },
      { label: 'Owners', prompt: 'list the owners' },
    ];
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({
      '4#Risks': 'list the risks',
      '5#Owners': 'list the owners',
    });
  });

  it('gives it the next free number wherever it sits in the list', () => {
    // The case that distinguishes "next free" from "position": an unnumbered
    // section ahead of the numbered ones. Taking its position would give it
    // `1#Risks`, colliding with the number `1#Summary` already holds, and the
    // order of the two on reload would depend on how the tie breaks.
    const templates = [{ label: 'Risks', prompt: 'list the risks' }, ...asTemplates(SHIPPED)];
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({
      '4#Risks': 'list the risks',
    });
  });

  it('stores nothing when no section carries a number and they match the shipped set', () => {
    const templates = Object.entries(SHIPPED).map(([key, prompt]) => ({
      label: key.split('#')[1],
      prompt,
    }));
    expect(buildCustomTemplateConfig(templates, SHIPPED)).toEqual({});
  });

  it('stores an explicit NONE a user typed', () => {
    const templates = asTemplates(SHIPPED);
    templates[1] = { ...templates[1], prompt: 'NONE' };
    expect(buildCustomTemplateConfig(templates, SHIPPED)['2#Details']).toBe('NONE');
  });

  it('turns every shipped section off when the page is emptied', () => {
    expect(buildCustomTemplateConfig([], SHIPPED)).toEqual({
      '1#Summary': 'NONE',
      '2#Details': 'NONE',
      '3#Actions': 'NONE',
    });
  });

  it('never stores the record version or the name of whoever saved it', () => {
    const stored = buildCustomTemplateConfig(asTemplates(SHIPPED), { ...SHIPPED, ...PROVENANCE });
    expect(stored).toEqual({});
  });

  it('round-trips: what a save stores reloads as what was on the page', () => {
    const edited = asTemplates(SHIPPED);
    edited[1] = { ...edited[1], prompt: 'my details prompt' };
    const stored = buildCustomTemplateConfig(edited, SHIPPED);
    expect(mergeTemplateConfig(SHIPPED, stored)).toEqual({
      '1#Summary': 'shipped summary prompt',
      '2#Details': 'my details prompt',
      '3#Actions': 'shipped actions prompt',
    });
  });

  it('round-trips a deletion: the section stays gone and the rest keep inheriting', () => {
    const remaining = asTemplates(SHIPPED).filter((entry) => entry.label !== 'Details');
    const stored = buildCustomTemplateConfig(remaining, SHIPPED);
    const reloaded = mergeTemplateConfig(SHIPPED, stored);
    expect(Object.keys(reloaded).sort()).toEqual(['1#Summary', '3#Actions']);

    // And the point of it: the two survivors are not in the stored profile at
    // all, so a release that improves either one still reaches this deployment.
    expect(stored['1#Summary']).toBeUndefined();
    expect(stored['3#Actions']).toBeUndefined();
    const improved = { ...SHIPPED, '3#Actions': 'a better actions prompt' };
    expect(mergeTemplateConfig(improved, stored)['3#Actions']).toBe('a better actions prompt');
  });
});

describe('telling a superseded save from an ordinary failure', () => {
  // The two outcomes are a message saying to reload and reapply, and one saying
  // to try again. Showing the first for a transient failure sends an
  // administrator off to reload for nothing; showing the second for a
  // superseded save invites them to click Save until it sticks, which is the
  // thing the conditional write exists to stop.
  it('recognises the refusal the resolver raises', () => {
    expect(
      isConflict({
        errors: [{ message: "Conflict: the configuration profile 'custom' was saved by someone" }],
      }),
    ).toBe(true);
  });

  it('recognises it on a plain error message too', () => {
    expect(isConflict({ message: 'Conflict: reload the page' })).toBe(true);
  });

  it('does not claim a conflict for an unrelated failure', () => {
    expect(isConflict({ errors: [{ message: 'Network error' }] })).toBe(false);
    expect(isConflict({ message: 'Failed to update LLM prompt template: timeout' })).toBe(false);
  });

  it('does not claim a conflict for an error that merely mentions the word', () => {
    // Matching the word anywhere would fire on this, and on a prompt that
    // happened to contain it coming back in an error message.
    expect(isConflict({ errors: [{ message: 'A scheduling Conflict prevented the request' }] })).toBe(false);
  });

  it('survives an error with nothing readable on it', () => {
    expect(isConflict(undefined)).toBe(false);
    expect(isConflict({})).toBe(false);
    expect(isConflict({ errors: [null] })).toBe(false);
  });
});
