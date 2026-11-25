/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which roles the transcriber admits.
 *
 * - The role rule is the role matrix's, vector for vector.
 * - The admitted roles are those of the matrix capability that lists this
 *   surface, so the transcriber and the matrix cannot drift apart.
 * - A token from before the pre token generation trigger carries no groups for
 *   a User and is still admitted; a Participant token is refused whatever else
 *   its groups hold, unless it also names Admin.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { TRANSCRIBER_ROLES, claimedGroups, effectiveRole, mayUseTranscriber } from './roles';

const REPO = path.resolve(__dirname, '../../../../..');

type Matrix = {
    effectiveRole: { vectors: Array<{ groups: string[]; role: string }> };
    capabilities: Array<{ roles: string[]; surfaces: string[] }>;
};

const matrix = JSON.parse(
    fs.readFileSync(path.join(REPO, 'lma-ai-stack/source/appsync/role-matrix.json'), 'utf8')
) as Matrix;

test('the role rule matches the role matrix', () => {
    for (const vector of matrix.effectiveRole.vectors) {
        assert.equal(effectiveRole(vector.groups), vector.role, `groups ${JSON.stringify(vector.groups)}`);
    }
});

test('the transcriber admits the roles the matrix lists for it', () => {
    const capability = matrix.capabilities.find((c) => c.surfaces.includes('transcriber-websocket'));
    assert.ok(capability, 'no capability in the role matrix lists the transcriber-websocket surface');
    assert.deepEqual([...TRANSCRIBER_ROLES].sort(), [...capability.roles].sort());
});

test('a Participant token is refused and an Admin or User token admitted', () => {
    assert.equal(mayUseTranscriber(claimedGroups({ 'cognito:groups': ['Participant'] })), false);
    assert.equal(mayUseTranscriber(claimedGroups({ 'cognito:groups': 'Participant' })), false);
    assert.equal(mayUseTranscriber(claimedGroups({ 'cognito:groups': ['Sales', 'Participant'] })), false);
    assert.equal(mayUseTranscriber(claimedGroups({ 'cognito:groups': ['Admin'] })), true);
    assert.equal(mayUseTranscriber(claimedGroups({ 'cognito:groups': ['User'] })), true);
});

test('a User token issued before the trigger, with no groups claim, is admitted', () => {
    assert.equal(mayUseTranscriber(claimedGroups({})), true);
    assert.equal(mayUseTranscriber(claimedGroups({ 'cognito:groups': '' })), true);
});
