/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/** The meeting pack's Summary PDF: the summary download's, built without the consent record. */
import { describe, it, expect, vi } from 'vitest';

import { defaultBuilders, PART_SUMMARY } from './meeting-pack';
import { summaryPdfBlob } from '../common/download-func';

vi.mock('../common/download-func', async (importOriginal) => ({
  ...(await importOriginal()),
  summaryPdfBlob: vi.fn(async () => new Blob(['%PDF-'])),
}));

describe('the Summary part', () => {
  it('is built without the consent record, with the names the page lists', async () => {
    const item = { callId: 'Weekly sync', callSummaryText: '{"SUMMARY":"ok"}' };
    const consent = { Mode: 'opt_in', Participants: ['Ana'] };
    await defaultBuilders[PART_SUMMARY]({ item, consent, participants: ['Ana Lima'] }, {});
    expect(summaryPdfBlob).toHaveBeenCalledWith(item, null, { participants: ['Ana Lima'] });
  });
});
