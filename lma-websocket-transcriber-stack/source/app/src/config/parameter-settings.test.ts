/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading a setting whose default is a stack parameter, in the shared accessor.
 *
 * The cases are the Python half's (lma_config/test_parameters.py): a value
 * reaches a reader from the configuration or from an environment variable, and
 * the two halves have to agree on what each one means.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { coerceSetting, parameterBackedSetting } from './configuration-store';

test('a value is read back into its type as the Python half reads it', () => {
    assert.equal(coerceSetting('string', 'us.amazon.nova-pro-v1:0'), 'us.amazon.nova-pro-v1:0');
    assert.equal(coerceSetting('string', ''), '');
    assert.equal(coerceSetting('integer', '90'), 90);
    assert.equal(coerceSetting('integer', ' 7 '), 7);
    assert.equal(coerceSetting('integer', 12), 12);
    assert.equal(coerceSetting('boolean', 'true'), true);
    assert.equal(coerceSetting('boolean', 'False'), false);
    assert.equal(coerceSetting('boolean', true), true);
});

test('a value of the wrong type is refused as the Python half refuses it', () => {
    assert.equal(coerceSetting('integer', 'ninety'), undefined);
    assert.equal(coerceSetting('integer', true), undefined);
    assert.equal(coerceSetting('integer', ''), undefined);
    assert.equal(coerceSetting('integer', '1_000'), undefined);
    assert.equal(coerceSetting('integer', '2.5'), undefined);
    assert.equal(coerceSetting('integer', 2.5), undefined);
    assert.equal(coerceSetting('boolean', 'yes'), undefined);
});

test('the section wins, then the environment variable, then the fallback', () => {
    assert.equal(parameterBackedSetting({ n: 5 }, 'n', 'integer', '3', 1), 5);
    assert.equal(parameterBackedSetting({}, 'n', 'integer', '3', 1), 3);
    assert.equal(parameterBackedSetting({}, 'n', 'integer', undefined, 1), 1);
});

test('a stored value of the wrong type falls back to the environment variable', () => {
    assert.equal(parameterBackedSetting({ n: 'many' }, 'n', 'integer', '3', 1), 3);
});

test('an empty environment variable is a value for text only', () => {
    assert.equal(parameterBackedSetting({}, 's', 'string', '', 'fallback'), '');
    assert.equal(parameterBackedSetting({}, 'n', 'integer', '', 4), 4);
    assert.equal(parameterBackedSetting({}, 'b', 'boolean', '', true), true);
});
