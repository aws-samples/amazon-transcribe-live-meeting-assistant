/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Upload Audio page says about an upload's language and its errors.
 *
 * createUploadMeeting answers with transcriptionLanguage: the language code the
 * recording is transcribed in, or identify-language /
 * identify-multiple-languages when Amazon Transcribe identifies it from the
 * Configuration page's language options. An older backend leaves it out.
 */
import { languageName } from '../common/detected-languages';
import { transcriptionLanguage } from '../common/meeting-context';

/**
 * How the completion message names the upload's language, or null when the
 * response does not say: "Korean (South Korea), ko-KR" for a language code, named
 * as the meeting page names detected languages, and the meeting page's wording
 * for an identify mode.
 */
export const uploadLanguageLabel = (code) => {
  const label = transcriptionLanguage({ transcriptionLanguage: code });
  if (!label) return null;
  const stored = code.trim();
  if (label !== stored) return label;
  const name = languageName(stored);
  // A code Intl cannot name comes back as itself ("qq (QQ)" for qq-QQ).
  const named = name && name.split(' ')[0] !== stored.split('-')[0];
  return named ? `${name}, ${stored}` : stored;
};

/**
 * The message to show for a failed upload. A GraphQL error, such as the
 * settings on the Configuration page that an upload cannot use, carries its
 * reason in `errors`; anything else is an Error or a value.
 */
export const uploadErrorMessage = (error) => {
  const messages = (Array.isArray(error?.errors) ? error.errors : [])
    .map((entry) => (typeof entry?.message === 'string' ? entry.message.trim() : ''))
    .filter(Boolean);
  if (messages.length) return messages.join(' ');
  if (typeof error?.message === 'string' && error.message.trim()) return error.message;
  return String(error);
};
