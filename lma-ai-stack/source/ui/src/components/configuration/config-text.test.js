/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The configuration as text, and the comparison of two profiles. What would be
 * silent if wrong is asserted most: a round trip that changes a value's type
 * (YAML reading "on" or "0123" as something other than text), a section the
 * text leaves out being emptied rather than kept, and a difference the
 * comparison misses because only one side has the key.
 */
import { describe, expect, it } from 'vitest';

import { compareProfiles, configurationText, readConfigurationText } from './config-text';

const sections = [{ name: 'language_models' }, { name: 'meeting_processing' }];

const values = {
  language_models: { summaryModelId: 'global.anthropic.claude-sonnet-5-5' },
  meeting_processing: { inactiveMeetingTimeoutMinutes: 240, transcriptHookNonPartialOnly: true },
};

describe('configurationText', () => {
  it('writes every section in the order the page lists them, as JSON', () => {
    const text = configurationText(sections, values, 'json');
    expect(Object.keys(JSON.parse(text))).toEqual(['language_models', 'meeting_processing']);
    expect(JSON.parse(text)).toEqual(values);
  });

  it('writes YAML that reads back to the same values', () => {
    const text = configurationText(sections, values, 'yaml');
    expect(text).toContain('inactiveMeetingTimeoutMinutes: 240');
    expect(readConfigurationText(text, 'yaml', sections).values).toEqual(values);
  });

  it('gives a section with no values an empty object', () => {
    expect(JSON.parse(configurationText(sections, {}, 'json'))).toEqual({
      language_models: {},
      meeting_processing: {},
    });
  });

  it('keeps text that looks like another type as text through a YAML round trip', () => {
    const tricky = { language_models: { a: 'on', b: '0123', c: 'null', d: '1e3', e: 'multi\nline' } };
    const text = configurationText([{ name: 'language_models' }], tricky, 'yaml');
    expect(readConfigurationText(text, 'yaml', [{ name: 'language_models' }]).values).toEqual(tricky);
  });
});

describe('readConfigurationText', () => {
  it('keeps a section the text leaves out', () => {
    const result = readConfigurationText('{"language_models": {"summaryModelId": "x"}}', 'json', sections);
    expect(result.values).toEqual({ language_models: { summaryModelId: 'x' } });
    expect(result.unchanged).toEqual(['meeting_processing']);
  });

  it('reads YAML booleans and numbers as the JSON types', () => {
    const result = readConfigurationText(
      'meeting_processing:\n  inactiveMeetingTimeoutMinutes: 30\n  transcriptHookNonPartialOnly: false\n',
      'yaml',
      sections,
    );
    expect(result.values.meeting_processing).toEqual({
      inactiveMeetingTimeoutMinutes: 30,
      transcriptHookNonPartialOnly: false,
    });
  });

  it('reads YAML with the JSON schema, so only JSON-style scalars become other types', () => {
    const result = readConfigurationText(
      'language_models:\n  a: True\n  b: 0x1F\n  c: ~\n  d: .inf\n  e: 2001-12-14\n',
      'yaml',
      [{ name: 'language_models' }],
    );
    expect(result.values.language_models).toEqual({ a: 'True', b: '0x1F', c: '~', d: '.inf', e: '2001-12-14' });
  });

  it('says where JSON that does not parse goes wrong', () => {
    expect(() => readConfigurationText('{"language_models": ', 'json', sections)).toThrow(/not valid JSON/);
  });

  it('says on which line YAML that does not parse goes wrong', () => {
    expect(() => readConfigurationText('language_models:\n  a: [1, 2\n', 'yaml', sections)).toThrow(
      /not valid YAML \(line \d+, column \d+\)/,
    );
  });

  it('refuses a document that is not an object of sections', () => {
    expect(() => readConfigurationText('[]', 'json', sections)).toThrow(/must be an object/);
    expect(() => readConfigurationText('just text', 'yaml', sections)).toThrow(/must be an object/);
  });

  it('names a section this deployment does not have', () => {
    expect(() => readConfigurationText('{"no_such_section": {}}', 'json', sections)).toThrow(
      /no section named no_such_section, or does not offer it/,
    );
  });

  it('refuses a section that is not an object of settings', () => {
    expect(() => readConfigurationText('{"language_models": "x"}', 'json', sections)).toThrow(/language_models is not/);
  });
});

describe('reading the text the way an import is read', () => {
  const engine = {
    name: 'asr_engine',
    schema: {
      properties: {
        diarizeVirtualParticipant: { type: 'boolean' },
        streamingEngineMicrovm: { type: 'boolean', availableWhen: { setting: 'AsrEngineAvailable', value: 'true' } },
      },
    },
  };
  const defaults = { asr_engine: { diarizeVirtualParticipant: false, streamingEngineMicrovm: false } };

  it('leaves a setting the deployment does not offer out of the text', () => {
    const text = configurationText([engine], defaults, 'json', {});
    expect(JSON.parse(text)).toEqual({ asr_engine: { diarizeVirtualParticipant: false } });
    expect(JSON.parse(configurationText([engine], defaults, 'json', { AsrEngineAvailable: 'true' }))).toEqual(defaults);
  });

  it('does not take a setting the deployment does not offer from the text, and says so', () => {
    const current = { asr_engine: { diarizeVirtualParticipant: false, streamingEngineMicrovm: false } };
    const result = readConfigurationText(
      '{"asr_engine": {"diarizeVirtualParticipant": true, "streamingEngineMicrovm": true}}',
      'json',
      [engine],
      defaults,
      {},
      current,
    );
    expect(result.values.asr_engine).toEqual({ diarizeVirtualParticipant: true, streamingEngineMicrovm: false });
    expect(result.withheld).toEqual([['asr_engine', 'streamingEngineMicrovm']]);
  });

  it('keeps the page value of a setting the text cannot hold', () => {
    const current = { asr_engine: { diarizeVirtualParticipant: false, streamingEngineMicrovm: true } };
    const result = readConfigurationText('{"asr_engine": {}}', 'json', [engine], defaults, {}, current);
    expect(result.values.asr_engine.streamingEngineMicrovm).toBe(true);
    expect(result.withheld).toEqual([]);
  });

  it('reads a setting removed from a merge section back as its default', () => {
    const models = { name: 'language_models', schema: { properties: { a: {}, b: {} } } };
    const result = readConfigurationText('{"language_models": {"a": "mine"}}', 'json', [models], {
      language_models: { a: 'shipped', b: 'shipped-b' },
    });
    expect(result.values.language_models).toEqual({ a: 'mine', b: 'shipped-b' });
  });
});

describe('compareProfiles', () => {
  it('lists each setting the two hold differently, in section order', () => {
    const other = {
      language_models: { summaryModelId: 'us.amazon.nova-pro-v1:0' },
      meeting_processing: { inactiveMeetingTimeoutMinutes: 240, transcriptHookNonPartialOnly: false },
    };
    expect(compareProfiles(sections, values, other)).toEqual([
      {
        section: 'language_models',
        path: ['summaryModelId'],
        left: 'global.anthropic.claude-sonnet-5-5',
        right: 'us.amazon.nova-pro-v1:0',
      },
      { section: 'meeting_processing', path: ['transcriptHookNonPartialOnly'], left: true, right: false },
    ]);
  });

  it('finds a setting only one side holds, whichever side it is', () => {
    const left = { language_models: { a: 1 } };
    const right = { language_models: { b: 2 } };
    expect(compareProfiles(sections, left, right)).toEqual([
      { section: 'language_models', path: ['a'], left: 1, right: undefined },
      { section: 'language_models', path: ['b'], left: undefined, right: 2 },
    ]);
  });

  it('compares nested settings key by key and a list as one value', () => {
    const left = { language_models: { prices: { us: { input: 1 } }, list: [1, 2] } };
    const right = { language_models: { prices: { us: { input: 2 } }, list: [2, 1] } };
    expect(compareProfiles(sections, left, right).map((entry) => entry.path)).toEqual([
      ['prices', 'us', 'input'],
      ['list'],
    ]);
  });

  it('finds nothing between equal profiles', () => {
    expect(compareProfiles(sections, values, JSON.parse(JSON.stringify(values)))).toEqual([]);
  });
});
