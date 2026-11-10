/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The language Amazon Transcribe identified for a result, or undefined when it
 * names none. A result carries `LanguageCode` only when the stream asked for
 * language identification (identify-language or identify-multiple-languages);
 * with a fixed language the field is absent, and so is the segment's.
 */
export const resultLanguageCode = (result: any): string | undefined => {
  const code = typeof result?.LanguageCode === 'string' ? result.LanguageCode.trim() : '';
  return code || undefined;
};
