/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The credentials the VP reads from Secrets Manager are registered with the
 * logger as they are read, so no later line can contain one.
 */
import { strict as assert } from 'node:assert';
import { afterEach, mock, test } from 'node:test';
import { SecretsManagerClient } from '@aws-sdk/client-secrets-manager';
import { REDACTED, captureLogs, clearSecretValuesForTest, createLogger } from './logger.js';
import { fetchZoomCredentials } from './zoom-login.js';
import { VirtualParticipantStatusManager } from './status-manager.js';

afterEach(() => {
    mock.restoreAll();
    clearSecretValuesForTest();
});

function written(text: string): string {
    const captured = captureLogs();
    try {
        createLogger('unit').info(text);
    } finally {
        captured.restore();
    }
    return captured.messages()[0];
}

test('the Zoom sign-in password is redacted from every line once it is fetched', async () => {
    mock.method(SecretsManagerClient.prototype, 'send', async () => ({
        SecretString: JSON.stringify({ username: 'bot@example.com', password: 'zoom-pass-789' }),
    }));

    await fetchZoomCredentials('zoom-secret');

    assert.equal(written('typed zoom-pass-789'), `typed ${REDACTED}`);
});

test('the origin-verify header value is redacted from every line once it is fetched', async () => {
    const previous = process.env.VNC_ORIGIN_VERIFY_SECRET_ARN;
    process.env.VNC_ORIGIN_VERIFY_SECRET_ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:origin';
    try {
        const manager = new VirtualParticipantStatusManager('vp-1');
        (manager as unknown as { secretsClient: { send: () => Promise<unknown> } }).secretsClient = {
            send: async () => ({ SecretString: JSON.stringify({ headersecret: 'origin-hdr-123456' }) }),
        };
        const value = await (manager as unknown as { getOriginVerifyHeaderValue: () => Promise<string | null> })
            .getOriginVerifyHeaderValue();
        assert.equal(value, 'origin-hdr-123456');
    } finally {
        if (previous === undefined) delete process.env.VNC_ORIGIN_VERIFY_SECRET_ARN;
        else process.env.VNC_ORIGIN_VERIFY_SECRET_ARN = previous;
    }

    assert.equal(written('header origin-hdr-123456'), `header ${REDACTED}`);
});
