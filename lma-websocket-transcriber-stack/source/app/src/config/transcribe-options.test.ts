/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * transcribeOptionsProblem against the table lma_config's tests read too
 * (test-fixtures/transcribe-option-combinations.json at the repository root),
 * so a streaming meeting and an upload refuse the same combinations.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { transcribeOptionsProblem, type TranscribeOptionCombination } from './configuration-store';

interface Case {
    readonly name: string;
    readonly options: TranscribeOptionCombination;
    readonly streaming: string | null;
}

// The same depth under the package from src/config and dist/config.
const FIXTURE = path.join(__dirname, '../../../../../test-fixtures/transcribe-option-combinations.json');
const CASES: readonly Case[] = JSON.parse(readFileSync(FIXTURE, 'utf-8')).cases;

test('the shared table has cases', () => {
    assert.ok(CASES.length > 0);
});

for (const fixtureCase of CASES) {
    test(`streaming: ${fixtureCase.name}`, () => {
        assert.equal(transcribeOptionsProblem(fixtureCase.options), fixtureCase.streaming ?? undefined);
    });
}
