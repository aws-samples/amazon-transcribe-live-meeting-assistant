// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { open, FileHandle } from 'fs/promises';
import { WriteStream, createWriteStream } from 'fs';
import { once } from 'events';

export type AudioFormat = 'PCMU' | 'L16';
export type SampleRate = 8000 | 16000 | 44100 | 48000;

const isLittleEndianHost = (new DataView(new Uint8Array([0xaa, 0x55]).buffer).getInt16(0, true) === 0x55aa);
export const defaultSupportedRates: readonly SampleRate[] = [8000, 16000, 44100, 48000] as const;


/**
 * Simple WAV file writer class
 * 
 * Only writes u-law or Linear16 (PCM) files.
 */
export class WavFileWriter {
    readonly filename: string;
    readonly format: AudioFormat;
    readonly rate: number;
    readonly channels: number;
    readonly bytesPerSample: number;
    private header: Buffer;
    private dataWriter: WriteStream | null = null;
    private closed = false;
    private writeError: Error | null = null;
    private closeHandlers: Array<{ resolve: (numSamples: number) => void, reject: (error: Error) => void }> = [];

    private constructor(filename: string, format: AudioFormat, rate: number, channels: number) {
        if((rate < 8000) || (rate > 192000)) {
            throw new RangeError(`Invalid sample rate: ${rate}`);
        }
        if((channels < 1) || (channels > 16)) {
            throw new RangeError(`Invalid number of channels: ${channels}`);
        }
        this.filename = filename;
        this.format = format;
        this.rate = rate;
        this.channels = channels | 0;
        if (this.format === 'PCMU') {
            this.bytesPerSample = this.channels;
            this.header = Buffer.alloc(58);
            this.header.write('RIFF', 0);
            this.header.writeUInt32LE(0xffffffff, 4);
            this.header.write('WAVEfmt ', 8);
            this.header.writeUInt32LE(16, 18);                          // chunk size
            this.header.writeUInt16LE(7, 20);                           // format = u-Law
            this.header.writeUInt16LE(this.channels, 22);               // num channels
            this.header.writeUInt32LE(this.rate, 24);                   // sample rate
            this.header.writeUInt32LE(this.rate*this.channels, 28);     // bytes per second
            this.header.writeUInt16LE(this.bytesPerSample, 32);         // block size (bytes per sample * num channels)
            this.header.writeUInt16LE(8, 34);                           // bits per sample
            this.header.writeUInt16LE(0, 36);                           // extension size
            this.header.write('fact', 38);
            this.header.writeUInt32LE(4, 42);
            this.header.writeUInt32LE(0xffffffff, 46);
            this.header.write('data', 50);
            this.header.writeUInt32LE(0xffffffff, 54);
        } else if (this.format === 'L16') {
            this.bytesPerSample = this.channels * 2;
            this.header = Buffer.alloc(44);
            this.header.write('RIFF', 0);
            this.header.writeUInt32LE(0xffffffff, 4);
            this.header.write('WAVEfmt ', 8);
            this.header.writeUInt32LE(16, 16);                          // chunk size
            this.header.writeUInt16LE(1, 20);                           // format = PCM
            this.header.writeUInt16LE(this.channels, 22);               // num channels
            this.header.writeUInt32LE(this.rate, 24);                   // sample rate
            this.header.writeUInt32LE(this.rate*this.channels*2, 28);   // bytes per second
            this.header.writeUInt16LE(this.bytesPerSample, 32);         // block size (bytes per sample * num channels)
            this.header.writeUInt16LE(16, 34);                          // bits per sample
            this.header.write('data', 36);
            this.header.writeUInt32LE(0xffffffff, 40);
        } else {
            throw new RangeError(`WAV writer: Unsupported format ${this.format}. Supported: PCMU and L16`);
        }
    }

    /**
     * Factory to create a WAV file
     * 
     * Opens file and writes header before resolving the returned Promise.
     * 
     * @param filename File path to the WAV file to create
     * @param format   Format of the WAV file. Supported: 'PCMU' and 'L16'
     * @param rate     Sample rate in samples per second
     * @param channels Number of channels
     * @returns Promise that resolves to a Writer object
     */
    static async create(filename: string, format: AudioFormat, rate: number, channels: number): Promise<WavFileWriter> {
        const writer = new WavFileWriter(filename, format, rate, channels);
        const file = await open(writer.filename, 'w');

        // Write the WAV header
        await file.write(writer.header, 0, writer.header.length, 0);

        const dataWriter = createWriteStream(writer.filename, { fd: file.fd, autoClose: false, start: writer.header.length });
        dataWriter.on('error', (err) => {
            writer.writeError = err;
        });
        dataWriter.on('finish', () => {
            // Update the header with the actual sizes 
            const pad = dataWriter.bytesWritten % 2;
            const numSamples = dataWriter.bytesWritten / writer.bytesPerSample;
            const fileEndPos = dataWriter.bytesWritten + writer.header.length;
            writer.header.writeUInt32LE(fileEndPos + pad - 8, 4);
            if (writer.format === 'PCMU') {
                writer.header.writeUInt32LE(numSamples, 46);                // fact chunk value (number of samples)
                writer.header.writeUInt32LE(dataWriter.bytesWritten, 54);   // data chunk size
            } else {
                writer.header.writeUInt32LE(dataWriter.bytesWritten, 40);   // data chunk size
            }

            // Patch the header with the actual sizes and close the file
            (async () => {
                try {
                    if(pad !== 0) {
                        await file.write(Buffer.alloc(pad), 0, pad, fileEndPos);
                    }
                    await file.write(writer.header, 0, writer.header.length, 0);
                } finally {
                    await file.close();
                }
            })().catch(error => {
                writer.writeError ??= error;
            }).finally(() => {
                writer.closed = true;
                if(writer.writeError) {
                    for(let h = writer.closeHandlers.pop(); h; h = writer.closeHandlers.pop()) {
                        h.reject(writer.writeError);
                    }
                } else {
                    for(let h = writer.closeHandlers.pop(); h; h = writer.closeHandlers.pop()) {
                        h.resolve(numSamples);
                    }
                }
            });
        });
        writer.dataWriter = dataWriter;
        return writer;
    }

    /**
     * Finalizes the WAV file
     * 
     * Updates the header and closes the file before resolving the returned promise.
     * 
     * @returns {Promise} Promise that resolves with number of samples written when file has been completed (header updated, file closed)
     */
    close(): Promise<number> {
        return new Promise((resolve, reject) => {
            if (this.closed) {
                if(this.writeError) {
                    reject(this.writeError);
                } else {
                    resolve(this.samplesWritten);
                }
            } else {
                this.closeHandlers.push({ resolve, reject });
                if (this.dataWriter) {
                    this.dataWriter.end();
                    this.dataWriter = null;
                }
            }
        });
    }

    /**
     * Writes audio samples to file
     * 
     * @param samples Audio samples to write to file 
     * @returns true - Samples buffered for writing; false - Samples buffered for writing but buffer above high watermark (use waitDrain)
     */
    writeAudio(samples: Uint8Array | Int16Array): boolean {
        if (samples instanceof Uint8Array) {
            return this.writeData(samples);
        } else if (isLittleEndianHost) {
            return this.writeData(new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength));
        } else {
            // Data and host have different endianness, convert through view
            const res = new Uint8Array(samples.byteLength);
            const view = new DataView(res.buffer);
            samples.forEach((sample, i) => view.setInt16(i * 2, sample, true));
            return this.writeData(res);
        }
    }

    writeData(data: Uint8Array): boolean {
        if (this.writeError) {
            throw this.writeError;
        }
        return this.dataWriter?.write(data) ?? false;
    }

    async waitDrain(): Promise<void> {
        if (this.dataWriter) {
            await once(this.dataWriter, 'drain');
        }
    }

    get samplesWritten(): number {
        if (this.dataWriter) {
            // Note: The header is not written through 'dataWriter'. It only counts what's written as "data" chunk
            return (this.dataWriter.bytesWritten / this.bytesPerSample);
        }
        return 0;
    }

    get duration(): number {
        return this.samplesWritten / this.rate;
    }
}

export interface WavReader {
    readonly format: AudioFormat;
    readonly rate: SampleRate;
    readonly channels: number;
    close(): Promise<void>;
    readNext(samples: number): Promise<Uint8Array | Int16Array | null>;
}

class WavFileReader implements WavReader {
    private file: FileHandle;
    private dataStartPos: number;
    private readPos: number;
    private dataChunkSize: number;
    private bytesPerSample: number;
    private maxFrameSamples: number;
    private bufferFactory: (bytes: number) => Int16Array | Uint8Array;
    readonly format: AudioFormat;
    readonly rate: SampleRate;
    readonly channels: number;

    constructor(file: FileHandle, startPos: number, dataChunkSize: number, format: AudioFormat, rate: SampleRate, channels: number) {
        this.file = file;
        this.format = format;
        this.rate = rate;
        this.channels = channels;
        this.dataStartPos = startPos;
        this.readPos = 0;
        this.dataChunkSize = dataChunkSize;
        this.maxFrameSamples = 10 * this.rate; // Read at most 10s at a time
        if (format === 'L16') {
            this.bytesPerSample = 2 * channels;
            this.bufferFactory = (bytes) => new Int16Array(bytes / 2);
        } else {
            this.bytesPerSample = channels;
            this.bufferFactory = (bytes) => new Uint8Array(bytes);
        }
    }

    async close(): Promise<void> {
        this.readPos = this.dataChunkSize;
        await this.file.close();
    }

    async readNext(samples: number): Promise<Uint8Array | Int16Array | null> {
        const ask = (
            (samples <= 0) ? (
                this.rate
            ) : (samples > this.maxFrameSamples) ? (
                this.maxFrameSamples
            ) : (
                samples
            )
        ) * this.bytesPerSample;
        const pos = this.readPos;
        const available = Math.min(this.dataChunkSize - pos, ask);
        if (available === 0) {
            return null;
        }
        this.readPos += available;
        const buf = this.bufferFactory(available);
        const res = await this.file.read({
            buffer: buf,
            position: this.dataStartPos + pos,
        });
        if (res.bytesRead !== available) {
            throw new Error('Corrupt file: Truncated data chunk.');
        }
        return buf;
    }
}


export type WavFileReaderOptions = {
    allowedRates?: readonly SampleRate[];
    channelMin?: number;
    channelMax?: number;
};

export const createWavFileReader = async (filename: string, options: WavFileReaderOptions): Promise<WavReader> => {
    let file: FileHandle | null = null;
    try {
        file = await open(filename, 'r');
        const headerSize = 44;
        const headerData = await file.read({
            buffer: Buffer.alloc(headerSize),
            offset: 0,
            length: headerSize,
            position: 0,
        });
        if (headerData.bytesRead !== headerSize) {
            throw new Error('File too small for valid WAV file');
        }
        const headerView = new DataView(headerData.buffer.buffer);
        if (headerView.getUint32(0, false) !== 0x52494646) {    // 'RIFF'
            throw new Error('Not a valid/supported WAV file (RIFF tag missing)');
        }
        if (headerView.getUint32(8, false) !== 0x57415645) {    // 'WAVE'
            throw new Error('Not a valid/supported WAV file (no WAVE chunk');
        }
        if (headerView.getUint32(12, false) !== 0x666d7420) {   // 'fmt '
            throw new Error('Not a valid/supported WAV file (no fmt chunk)');
        }
        const fmtChunkSize = headerView.getUint32(16, true);
        if ((fmtChunkSize !== 16) && (fmtChunkSize !== 18) && (fmtChunkSize !== 40)) {
            throw new Error('Not a valid/supported WAV file (bad fmt chunk size)');
        }

        const formatTag = headerView.getUint16(20, true);
        let format: AudioFormat;
        if (formatTag === 1) {
            format = 'L16';
        } else if (formatTag === 7) {
            format = 'PCMU';
        } else {
            throw new Error(`Unsupported WAV format tag (${formatTag}). Only supporting 1(L16) and 7(PCMU)`);
        }

        const channels = headerView.getUint16(22, true);
        if ((channels < (options.channelMin ?? 1)) || (channels > (options.channelMax ?? 16))) {
            throw new Error(`Invalid number of channels: ${channels}`);
        }
        const rate = headerView.getUint32(24, true);
        const allowedRates: readonly number[] = (options.allowedRates ?? defaultSupportedRates);
        if (!allowedRates.includes(rate)) {
            throw new Error(`Unsupported sample rate (${rate}). Supported: ${allowedRates.join(',')}`);
        }
        const bytesPerSample = headerView.getUint16(32, true);
        if (bytesPerSample !== (channels * ((formatTag === 1) ? 2 : 1))) {
            throw new Error(`Invalid bytesPerSample for ${channels} channel ${format}`);
        }

        // Now search for 'data' chunk
        // Start after 'fmt ' chunk
        let pos = 20 + fmtChunkSize;
        const chunk = Buffer.alloc(8);
        for(;;) {
            const res = await file.read({
                buffer: chunk,
                offset: 0,
                length: chunk.byteLength,
                position: pos,
            });
            if(res.bytesRead !== chunk.byteLength) {
                throw new Error('Corrupt or invalid WAV file (no data chunk found)');
            }
            const view = new DataView(chunk.buffer);
            let dataSize = view.getUint32(4, true);
            if (view.getUint32(0, false) === 0x64617461) {      // 'data' chunk
                if(dataSize === 0xffffffff) {
                    // Data chunk spans to end of file
                    const stats = await file.stat();
                    dataSize = stats.size - 8 - pos;
                }
                const reader = new WavFileReader(file, pos + 8, dataSize, format, rate as SampleRate, channels);
                file = null;    // File is now owned by reader
                return reader;
            }
            pos += 8 + dataSize + (dataSize % 2);
        }
    } finally {
        await file?.close();
    }
};
