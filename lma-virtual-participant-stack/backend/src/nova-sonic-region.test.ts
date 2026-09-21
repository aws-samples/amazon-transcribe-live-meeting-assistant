/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which region the Virtual Participant reaches Amazon Nova Sonic in (GitHub #508).
 *
 * Nova Sonic is available in fewer regions than LMA itself, so a deployment pinned
 * to one region for compliance needs to keep everything else local and reach the
 * voice assistant elsewhere. The precedence matters in both directions: an existing
 * deployment must be completely unaffected (the variable is empty or absent), and a
 * deployment that sets it must not be silently ignored — the failure would be a
 * voice assistant that simply does not respond, with an IAM denial in the log.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { resolveNovaSonicRegion } from './nova-agent.js';

test("an unset override uses the stack's own region", () => {
    // Every deployment that does not set the parameter must land here.
    assert.equal(resolveNovaSonicRegion(undefined, { AWS_REGION: 'eu-central-1' }), 'eu-central-1');
});

test('an empty override uses the stack region, not the built-in default', () => {
    // The parameter defaults to "", and CloudFormation passes that through as an
    // empty environment variable on the ECS launch types. Treating "" as a region
    // would send every default deployment to us-east-1.
    for (const empty of ['', '   ']) {
        assert.equal(
            resolveNovaSonicRegion(undefined, {
                AMAZON_NOVA_SONIC_REGION: empty,
                AWS_REGION: 'eu-central-1',
            }),
            'eu-central-1',
            `"${empty}" must not be taken as a region`,
        );
    }
});

test('the override is used when set', () => {
    // The reported case: the stack is in eu-central-1 for compliance, Nova Sonic is
    // reached in eu-north-1.
    assert.equal(
        resolveNovaSonicRegion(undefined, {
            AMAZON_NOVA_SONIC_REGION: 'eu-north-1',
            AWS_REGION: 'eu-central-1',
        }),
        'eu-north-1',
    );
});

test('surrounding whitespace on the override is tolerated', () => {
    assert.equal(
        resolveNovaSonicRegion(undefined, {
            AMAZON_NOVA_SONIC_REGION: '  eu-north-1  ',
            AWS_REGION: 'eu-central-1',
        }),
        'eu-north-1',
    );
});

test('an explicitly passed region still wins over the environment', () => {
    // A caller that already knows where to go must not be redirected.
    assert.equal(
        resolveNovaSonicRegion('us-west-2', {
            AMAZON_NOVA_SONIC_REGION: 'eu-north-1',
            AWS_REGION: 'eu-central-1',
        }),
        'us-west-2',
    );
});

test('with nothing at all set, the built-in default applies', () => {
    // A container run locally with no AWS_REGION.
    assert.equal(resolveNovaSonicRegion(undefined, {}), 'us-east-1');
});
