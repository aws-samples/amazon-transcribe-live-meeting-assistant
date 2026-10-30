/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The languages `LMA translate <language>` accepts, and the meeting language
 * the other half of the pair comes from.
 *
 * The list is the set of languages Amazon Nova 2 Sonic — the Virtual
 * Participant's default voice model — documents speech support for: English,
 * French, Italian, German, Spanish, Portuguese and Hindi
 * (https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-language-support.html).
 * Nova Sonic v1 covers the first five only. The translator prompt names its
 * two languages in English ("between English and French"), so each entry's
 * canonical name is the one the prompt is written with, and the aliases are
 * what a participant is likely to type: the name in the language itself and
 * in the other supported languages, with or without accents.
 *
 * Deliberately a closed list rather than any word after "translate": a typo or
 * a language the model cannot speak would otherwise be written into the
 * prompt, and the assistant would go quiet or answer in the wrong language
 * with nothing in the chat to say why.
 */
import { normalizeChatText } from './chat-text.js';

export interface TranslatorLanguage {
    /** The name the translator prompt uses, and the one replies show. */
    readonly name: string;
    /** ISO 639-1 code, matched against the Transcribe language code's prefix. */
    readonly code: string;
    /** Other ways to type it, already folded (lower case, no accents). */
    readonly aliases: readonly string[];
}

export const TRANSLATOR_LANGUAGES: ReadonlyArray<TranslatorLanguage> = [
    {
        name: 'English',
        code: 'en',
        aliases: ['ingles', 'anglais', 'englisch', 'inglese', 'american english', 'british english'],
    },
    {
        name: 'Spanish',
        code: 'es',
        aliases: ['espanol', 'castellano', 'espagnol', 'spanisch', 'spagnolo', 'espanhol'],
    },
    {
        name: 'French',
        code: 'fr',
        aliases: ['francais', 'frances', 'franzosisch', 'franzoesisch', 'francese'],
    },
    {
        name: 'German',
        code: 'de',
        aliases: ['deutsch', 'aleman', 'allemand', 'tedesco', 'alemao'],
    },
    {
        name: 'Italian',
        code: 'it',
        aliases: ['italiano', 'italien', 'italienisch'],
    },
    {
        name: 'Portuguese',
        code: 'pt',
        aliases: ['portugues', 'portugais', 'portugiesisch', 'portoghese', 'brazilian portuguese'],
    },
    {
        name: 'Hindi',
        code: 'hi',
        aliases: [],
    },
];

/** Lower case, accents removed, punctuation reduced to single spaces. */
export function foldLanguageText(text: string): string {
    return normalizeChatText(text.normalize('NFD').replace(/[\u0300-\u036f]/g, ''));
}

const LANGUAGE_INDEX: ReadonlyMap<string, TranslatorLanguage> = new Map(
    TRANSLATOR_LANGUAGES.flatMap((language) =>
        [language.name, ...language.aliases].map((alias) => [foldLanguageText(alias), language] as const),
    ),
);

/** The supported language a participant typed, or null. Case- and
 *  accent-insensitive, so "Español", "espanol" and "SPANISH" all match. */
export function resolveTranslatorLanguage(typed: string): TranslatorLanguage | null {
    return LANGUAGE_INDEX.get(foldLanguageText(typed)) ?? null;
}

/** The supported language names, for a reply that names them. */
export function supportedTranslatorLanguageNames(): string[] {
    return TRANSLATOR_LANGUAGES.map((language) => language.name);
}

/**
 * The meeting's own language, taken from how it is being transcribed.
 *
 * A fixed Transcribe language code (`en-US`, `fr-FR`) names it directly. With
 * language identification (`identify-language`, `identify-multiple-languages`)
 * there is no single code, so the preferred language is used when one is set;
 * otherwise null, and the caller keeps the configured first language.
 */
export function meetingLanguage(
    transcribeLanguageCode: string,
    preferredLanguage: string = '',
): TranslatorLanguage | null {
    const code = /^identify/i.test(transcribeLanguageCode) ? preferredLanguage : transcribeLanguageCode;
    const prefix = (code || '').toLowerCase().split('-')[0];
    return TRANSLATOR_LANGUAGES.find((language) => language.code === prefix) ?? null;
}
