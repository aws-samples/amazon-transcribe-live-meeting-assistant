/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import SectionNotice, { bundleLanguages, transcribeOnlyOptions } from './section-notices';

const WITH_ENGINE = { AsrEngineAvailable: 'true' };
// The default bundle's languages as the transcriber stack publishes them.
const PARAKEET_V3 = 'bg,cs,da,de,en,es,et,fi,fr,hr,hu,it,nl,pl,pt,ro,ru,sk,sv,uk';
const PARAKEET_V3_LANGUAGES = PARAKEET_V3.split(',');
const KEEPS_ON_TRANSCRIBE = 'These settings keep meetings on Amazon Transcribe';

describe('transcribeOnlyOptions', () => {
  it('names nothing for plain Amazon Transcribe options', () => {
    expect(transcribeOnlyOptions({ languageCode: 'en-US', contentRedactionEnabled: false })).toEqual([]);
    expect(transcribeOnlyOptions(undefined)).toEqual([]);
  });

  it.each([
    [{ contentRedactionEnabled: true }, 'content redaction'],
    [{ customVocabularyName: 'product-names' }, 'a custom vocabulary'],
    [{ customLanguageModelName: 'clm-1' }, 'a custom language model'],
    [{ languageCode: 'identify-language' }, 'language identification'],
    [{ languageCode: 'identify-multiple-languages' }, 'language identification'],
    [{ languageCode: 'de-DE' }, 'the language de-DE'],
    [{ languageCode: 'es-US' }, 'the language es-US'],
    [{ languageCode: 'ja-JP' }, 'the language ja-JP'],
  ])('names %o as %s', (transcription, name) => {
    expect(transcribeOnlyOptions(transcription)).toEqual([name]);
  });

  it.each(['en-US', 'en-GB', 'en-AU', undefined])('names nothing for the English language %s', (languageCode) => {
    expect(transcribeOnlyOptions({ languageCode })).toEqual([]);
  });

  it.each(['es-US', 'de-DE', 'fr-FR'])('names nothing for %s when the bundle transcribes it', (languageCode) => {
    expect(transcribeOnlyOptions({ languageCode }, PARAKEET_V3_LANGUAGES)).toEqual([]);
  });

  it('names a language the deployed bundle does not transcribe', () => {
    expect(transcribeOnlyOptions({ languageCode: 'ja-JP' }, PARAKEET_V3_LANGUAGES)).toEqual(['the language ja-JP']);
  });

  it('names language identification whatever the bundle transcribes', () => {
    expect(transcribeOnlyOptions({ languageCode: 'identify-language' }, PARAKEET_V3_LANGUAGES)).toEqual([
      'language identification',
    ]);
  });
});

describe('bundleLanguages', () => {
  it('reads the published list as lower-case base codes', () => {
    expect(bundleLanguages({ AsrModelLanguages: ' en, ES-us ,,fr ' })).toEqual(['en', 'es', 'fr']);
  });

  it('is empty, meaning English only, when nothing is published', () => {
    expect(bundleLanguages({})).toEqual([]);
    expect(bundleLanguages(undefined)).toEqual([]);
  });
});

describe('the Transcription Engine notice', () => {
  it('says the Amazon Transcribe-only options keep meetings on Amazon Transcribe', () => {
    render(<SectionNotice section="asr_engine" settings={WITH_ENGINE} configuration={{}} />);
    expect(screen.getByText(/meetings\s+stay on Amazon Transcribe whichever engine is selected here/)).toBeTruthy();
    expect(screen.queryByText(KEEPS_ON_TRANSCRIBE)).toBeNull();
  });

  it('warns when the saved Amazon Transcribe options use one of them', () => {
    render(
      <SectionNotice
        section="asr_engine"
        settings={WITH_ENGINE}
        configuration={{ transcription: { contentRedactionEnabled: true, customVocabularyName: 'v' } }}
      />,
    );
    expect(screen.getByText(KEEPS_ON_TRANSCRIBE)).toBeTruthy();
    expect(screen.getByText(/uses content redaction, a custom vocabulary, so every streaming meeting/)).toBeTruthy();
  });

  it('warns about a language other than English', () => {
    render(
      <SectionNotice
        section="asr_engine"
        settings={WITH_ENGINE}
        configuration={{ transcription: { languageCode: 'fr-FR' } }}
      />,
    );
    expect(
      screen.getByText(/uses the language fr-FR, so every streaming meeting and Virtual Participant is/),
    ).toBeTruthy();
  });

  it('does not warn about a language the deployed bundle transcribes, and lists its languages', () => {
    render(
      <SectionNotice
        section="asr_engine"
        settings={{ ...WITH_ENGINE, AsrModelLanguages: PARAKEET_V3 }}
        configuration={{ transcription: { languageCode: 'es-US' } }}
      />,
    );
    expect(screen.queryByText(KEEPS_ON_TRANSCRIBE)).toBeNull();
    expect(screen.getByText(/^English, .*Spanish/)).toBeTruthy();
  });

  it('lists English alone when the deployment publishes no languages', () => {
    render(<SectionNotice section="asr_engine" settings={WITH_ENGINE} configuration={{}} />);
    expect(screen.getByText('English')).toBeTruthy();
  });

  it('names only streaming meetings when a custom language model is the only such option', () => {
    render(
      <SectionNotice
        section="asr_engine"
        settings={WITH_ENGINE}
        configuration={{ transcription: { languageCode: 'en-US', customLanguageModelName: 'clm-1' } }}
      />,
    );
    expect(screen.getByText(/uses a custom language model, so every streaming meeting is transcribed/)).toBeTruthy();
    expect(screen.queryByText(/Virtual Participant is transcribed/)).toBeNull();
  });

  it('does not warn on a deployment without the engine', () => {
    render(
      <SectionNotice
        section="asr_engine"
        settings={{}}
        configuration={{ transcription: { contentRedactionEnabled: true } }}
      />,
    );
    expect(screen.queryByText(KEEPS_ON_TRANSCRIBE)).toBeNull();
    expect(screen.getByText('Every meeting is transcribed by Amazon Transcribe')).toBeTruthy();
  });
});
