/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The languages a meeting was spoken in, as the speech engine identified them,
 * and how the meeting page names them. Kept free of React so it can be tested
 * directly.
 *
 * Amazon Transcribe names a language per result when language identification is
 * on (the identify-language and identify-multiple-languages settings of the
 * Amazon Transcribe section); the producers put it on each transcript segment as
 * LanguageCode, and the call event processor lists each new one on the meeting
 * record as DetectedLanguages, in the order first heard. The ASR MicroVM engine,
 * with a multilingual bundle, names the language of each final row from its text
 * the same way. A fixed language on Amazon Transcribe names none, so such a
 * meeting has neither.
 *
 * A segment is marked with its language only when the meeting has more than one,
 * so a meeting in one language reads exactly as before. The mark is a short text
 * code ("ES"), not a flag: a flag names a country, not a language -- Spanish in
 * the United States would show the American flag -- and Windows renders the
 * regional-indicator flag emoji as two letters anyway.
 */

const displayNames = (style) => {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language', languageDisplay: 'standard', style });
  } catch {
    return null;
  }
};

const LONG_NAMES = displayNames('long');
const SHORT_NAMES = displayNames('short');

const known = (code) => (typeof code === 'string' && code.trim() ? code.trim() : null);

const nameWith = (names, code) => {
  const tag = known(code);
  if (!tag) return '';
  try {
    return names?.of(tag) || tag;
  } catch {
    // Not a well-formed language tag: shown as stored.
    return tag;
  }
};

/** The language's full English name, e.g. "Spanish (United States)" for es-US. */
export const languageName = (code) => nameWith(LONG_NAMES, code);

/** The language's shorter English name, e.g. "Spanish (US)" for es-US. */
export const shortLanguageName = (code) => nameWith(SHORT_NAMES, code);

/**
 * The meeting's languages, in the order first heard: the meeting record's
 * DetectedLanguages, then any its segments carry that the record does not list
 * yet (the record is updated a moment after the first segment in a language, and
 * the page may hold the record from before).
 */
export const meetingLanguages = (detectedLanguages, segments = []) => {
  const languages = [];
  const add = (code) => {
    const tag = known(code);
    if (tag && !languages.includes(tag)) languages.push(tag);
  };
  (Array.isArray(detectedLanguages) ? detectedLanguages : []).forEach(add);
  (segments || []).forEach((segment) => add(segment?.languageCode));
  return languages;
};

/** Whether the transcript marks each segment with its language: when the meeting has more than one. */
export const showsLanguageMarks = (languages) => (languages || []).length > 1;

/**
 * The short code a segment's mark shows: the language subtag ("ES" for es-US),
 * or the whole code ("EN-GB") when another of the meeting's languages shares the
 * subtag, so English (UK) and English (US) are told apart.
 */
export const languageMark = (code, languages = []) => {
  const tag = known(code);
  if (!tag) return '';
  const primary = tag.split('-')[0].toLowerCase();
  const shared = (languages || []).some((other) => other !== tag && other.split('-')[0].toLowerCase() === primary);
  return (shared ? tag : primary).toUpperCase();
};

/**
 * The mark for one segment, or null when it gets none: { text, name }, with
 * `name` the language's full name for the tooltip and the accessible label.
 */
export const segmentLanguageMark = (segment, languages) => {
  const tag = known(segment?.languageCode);
  if (!tag || !showsLanguageMarks(languages)) return null;
  return { text: languageMark(tag, languages), name: languageName(tag) };
};

/** The meeting's languages by name, for the Details tab: "English (US), Spanish (US)". */
export const detectedLanguagesText = (languages) => (languages || []).map(shortLanguageName).join(', ');
