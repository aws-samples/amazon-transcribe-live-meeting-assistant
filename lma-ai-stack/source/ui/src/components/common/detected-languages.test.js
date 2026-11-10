/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  detectedLanguagesText,
  languageMark,
  languageName,
  meetingLanguages,
  segmentLanguageMark,
  shortLanguageName,
  showsLanguageMarks,
} from './detected-languages';

describe('meetingLanguages', () => {
  it("lists the record's languages first, then the ones only the segments carry, each once", () => {
    const segments = [{ languageCode: 'es-US' }, { languageCode: 'en-US' }, {}, { languageCode: 'fr-FR' }];
    expect(meetingLanguages(['en-US'], segments)).toEqual(['en-US', 'es-US', 'fr-FR']);
  });

  it('is empty for a meeting with no identified language', () => {
    expect(meetingLanguages(undefined, [{ transcript: 'hello' }])).toEqual([]);
    expect(meetingLanguages(null)).toEqual([]);
  });
});

describe('which lines are marked', () => {
  it('marks lines only in a meeting with more than one language', () => {
    expect(showsLanguageMarks(['en-US'])).toBe(false);
    expect(showsLanguageMarks([])).toBe(false);
    expect(showsLanguageMarks(['en-US', 'es-US'])).toBe(true);
  });

  it('gives a line with no language no mark, and none in a meeting with one language', () => {
    expect(segmentLanguageMark({ transcript: 'x' }, ['en-US', 'es-US'])).toBeNull();
    expect(segmentLanguageMark({ languageCode: 'en-US' }, ['en-US'])).toBeNull();
  });

  it("names the line's language in full for its tooltip", () => {
    expect(segmentLanguageMark({ languageCode: 'es-US' }, ['en-US', 'es-US'])).toEqual({
      text: 'ES',
      name: 'Spanish (United States)',
    });
  });
});

describe('languageMark', () => {
  it('is the language subtag', () => {
    expect(languageMark('es-US', ['en-US', 'es-US'])).toBe('ES');
  });

  it('is the whole code when two of the meeting languages share a subtag', () => {
    expect(languageMark('en-GB', ['en-US', 'en-GB'])).toBe('EN-GB');
    expect(languageMark('en-US', ['en-US', 'en-GB', 'es-US'])).toBe('EN-US');
    expect(languageMark('es-US', ['en-US', 'en-GB', 'es-US'])).toBe('ES');
  });
});

describe('language names', () => {
  it('reads codes as English names, long and short', () => {
    expect(languageName('es-US')).toBe('Spanish (United States)');
    expect(shortLanguageName('en-US')).toBe('English (US)');
    expect(detectedLanguagesText(['en-US', 'es-US'])).toBe('English (US), Spanish (US)');
  });

  it('shows a code that is not a language tag as stored', () => {
    expect(languageName('not a tag')).toBe('not a tag');
    expect(languageName('')).toBe('');
  });
});
