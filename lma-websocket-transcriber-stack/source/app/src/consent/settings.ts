/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Meeting Consent section of the configuration, as this transcriber reads it:
 * at every meeting's START and for every client's CONSENT_SETTINGS request, on
 * the immediate tier (no cache, a consistent read), because it decides whether
 * a meeting may be captured.
 */
import { ConfigurationStore, declareSection } from '../config/configuration-store';
import { normalizeErrorForLogging } from '../utils/common';
import { ConsentSettings, effectiveSettings } from './rules';

/** Immediate, as in the Python half: consent-bearing. */
export const MEETING_CONSENT = declareSection({
    name: 'meeting_consent',
    cacheTier: 'immediate',
    description:
        'Meeting consent: the mode (off, notify or opt-in), the opt-in rule, the notice texts, the consent and ' +
        'objection phrases, whether a meeting may use another choice, and what to do with a client that sends no ' +
        'consent step.',
});

let store: ConfigurationStore | undefined;

/** Exposed for tests: read through this store instead of building one. */
export const useConsentSettingsStore = (replacement: ConfigurationStore | undefined): void => {
    store = replacement;
};

/**
 * The deployment's consent settings, or undefined when they could not be read.
 * A task with no configuration table has the shipped settings, in which consent
 * is off.
 */
export const readConsentSettings = async (log: {
    warn(message: string): void;
}): Promise<ConsentSettings | undefined> => {
    const tableName = process.env['CONFIGURATION_TABLE_NAME'] || '';
    if (!tableName && !store) {
        return effectiveSettings({});
    }
    try {
        store = store ?? new ConfigurationStore({ tableName });
        const section = await store.getSection<Record<string, unknown>>(MEETING_CONSENT);
        return effectiveSettings(section.values);
    } catch (error) {
        log.warn(`[CONSENT]: could not read the meeting consent settings: ${normalizeErrorForLogging(error)}`);
        return undefined;
    }
};
