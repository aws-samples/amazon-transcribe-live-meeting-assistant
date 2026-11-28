/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The page's playbook rules (playbook-model.js) hold the same names and limits
 * as the server's (lma_playbooks/rules.py), which decide: a check, an
 * assertiveness or a limit changed on one side alone fails here. The page is
 * stricter in one way only, on purpose: it does not save a playbook that turns
 * nothing on. See docs/meeting-playbooks.md.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { describe, expect, it } from 'vitest';

import { ASSERTIVENESS, GUIDANCE_CHECKS, LIMITS } from './playbook-model';

const RULES = readFileSync(
  join(import.meta.dirname, '../../../../lambda_layers/configuration_layer/lma_playbooks/rules.py'),
  'utf8',
);

const constant = (name) => {
  const match = RULES.match(new RegExp(`^${name} = (\\d+)`, 'm'));
  return match ? Number(match[1]) : undefined;
};

const dictKeys = (name) => {
  const start = RULES.indexOf(`${name}: Dict[str,`);
  const body = RULES.slice(start, RULES.indexOf('}', start));
  return [...body.matchAll(/^\s+"([a-z_]+)":/gm)].map((m) => m[1]);
};

describe('the playbook rules on the page and on the server', () => {
  it('name the same live guidance checks, in the same order', () => {
    expect(GUIDANCE_CHECKS.map((c) => c.value)).toEqual(dictKeys('GUIDANCE_CHECKS'));
  });

  it('name the same assertiveness levels', () => {
    const inline = RULES.match(/^ASSERTIVENESS: Dict\[str, float\] = \{([^}]*)\}/m)[1];
    expect(ASSERTIVENESS.map((a) => a.value)).toEqual([...inline.matchAll(/"([a-z]+)":/g)].map((m) => m[1]));
  });

  it('hold the same limits', () => {
    expect(LIMITS.whiteboards).toBe(constant('MAX_WHITEBOARDS'));
    expect(LIMITS.quickActions).toBe(constant('MAX_QUICK_ACTIONS'));
    expect(LIMITS.quickActionLabel).toBe(constant('MAX_QUICK_ACTION_LABEL_CHARS'));
    expect(LIMITS.quickActionPrompt).toBe(constant('MAX_QUICK_ACTION_PROMPT_CHARS'));
    expect(LIMITS.suggestPhrases).toBe(constant('MAX_SUGGEST_PHRASES'));
    expect(LIMITS.suggestPhrase).toBe(constant('MAX_SUGGEST_PHRASE_CHARS'));
    const items = RULES.match(/^MIN_ITEMS_PER_10_MIN, MAX_ITEMS_PER_10_MIN = (\d+), (\d+)/m);
    expect([LIMITS.minItems, LIMITS.maxItems]).toEqual([Number(items[1]), Number(items[2])]);
  });
});
