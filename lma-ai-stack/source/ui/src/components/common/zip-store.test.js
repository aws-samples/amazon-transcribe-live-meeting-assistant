/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/** The meeting pack's ZIP writer: stored files, UTF-8 names, and a directory every reader finds. */
import { describe, it, expect } from 'vitest';

import { crc32, zipFiles } from './zip-store';

const bytesOf = async (blob) => new Uint8Array(await blob.arrayBuffer());

describe('zipFiles', () => {
  it('computes the CRC-32 ZIP records', () => {
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926);
  });

  it('stores each file under its UTF-8 name, with a central directory pointing at it', async () => {
    const files = [
      { name: 'Summary.pdf', data: new TextEncoder().encode('%PDF-a') },
      { name: 'Whiteboards-計画.pdf', data: new TextEncoder().encode('%PDF-bb') },
    ];
    const blob = zipFiles(files, { date: new Date(2026, 10, 28, 15, 30, 10) });
    expect(blob.type).toBe('application/zip');
    const bytes = await bytesOf(blob);
    const view = new DataView(bytes.buffer);
    const end = bytes.length - 22;
    expect(view.getUint32(end, true)).toBe(0x06054b50);
    expect(view.getUint16(end + 10, true)).toBe(2);
    let at = view.getUint32(end + 16, true);
    const decoder = new TextDecoder();
    const found = [];
    for (let i = 0; i < 2; i += 1) {
      expect(view.getUint32(at, true)).toBe(0x02014b50);
      expect(view.getUint16(at + 8, true)).toBe(0x0800);
      expect(view.getUint16(at + 10, true)).toBe(0);
      const size = view.getUint32(at + 24, true);
      const nameLength = view.getUint16(at + 28, true);
      const local = view.getUint32(at + 42, true);
      const name = decoder.decode(bytes.subarray(at + 46, at + 46 + nameLength));
      expect(view.getUint32(local, true)).toBe(0x04034b50);
      const dataAt = local + 30 + view.getUint16(local + 26, true);
      const data = bytes.subarray(dataAt, dataAt + size);
      expect(crc32(data)).toBe(view.getUint32(at + 16, true));
      found.push([name, decoder.decode(data)]);
      at += 46 + nameLength;
    }
    expect(found).toEqual([
      ['Summary.pdf', '%PDF-a'],
      ['Whiteboards-計画.pdf', '%PDF-bb'],
    ]);
  });
});
