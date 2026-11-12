/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The language parameters of the Virtual Participant's Amazon Transcribe
 * streaming request (GitHub #766): the stack's "not set" value for the
 * preferred language, `None`, is never sent as a language code.
 */
import { strict as assert } from 'node:assert';
import { afterEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { details } from './details.js';
// eslint-disable-next-line import/first
import { transcribeLanguageParams } from './scribe.js';

const saved = {
    code: details.transcribeLanguageCode,
    options: details.transcribeLanguageOptions,
    preferred: details.transcribePreferredLanguage,
};

const configure = (code: string, options: string, preferred: string): void => {
    details.transcribeLanguageCode = code;
    details.transcribeLanguageOptions = options;
    details.transcribePreferredLanguage = preferred;
};

describe('transcribeLanguageParams preferred language', () => {
    afterEach(() => configure(saved.code, saved.options, saved.preferred));

    for (const code of ['identify-language', 'identify-multiple-languages']) {
        for (const preferred of ['None', 'none', ' NONE ', '']) {
            it(`omits PreferredLanguage for ${code} when it is ${JSON.stringify(preferred)}`, () => {
                configure(code, 'en-US, zh-CN', preferred);
                const { params, preferredLang } = transcribeLanguageParams();
                assert.equal('PreferredLanguage' in params, false);
                assert.equal(preferredLang, '');
                assert.equal(params.LanguageOptions, 'en-US,zh-CN');
            });
        }

        it(`sends a real preferred language for ${code}`, () => {
            configure(code, 'en-US,zh-CN', ' zh-CN ');
            const { params, preferredLang } = transcribeLanguageParams();
            assert.equal(params.PreferredLanguage, 'zh-CN');
            assert.equal(preferredLang, 'zh-CN');
        });
    }

    it('never sends PreferredLanguage for a fixed language', () => {
        configure('en-US', '', 'en-US');
        const { params } = transcribeLanguageParams();
        assert.equal('PreferredLanguage' in params, false);
        assert.equal(params.LanguageCode, 'en-US');
    });
});
