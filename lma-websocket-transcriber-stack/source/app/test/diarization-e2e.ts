/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * End-to-end check for per-channel diarization: streams a stereo fixture to the
 * REAL Amazon Transcribe streaming API, then feeds every TranscriptEvent through
 * the REAL `writeTranscriptionSegment` and reports the `Speaker` strings that
 * would land on the Kinesis stream.
 *
 * Complements src/calleventdata/diarization.test.ts, which unit-tests the pure
 * helpers offline. This script exercises the parts the unit tests cannot: the
 * turn splitting against labels Transcribe actually returned for real audio, the
 * ch_0 -> CALLER / ch_1 -> AGENT channel mapping, the interaction with the
 * existing activeSpeaker/agentId base labels, and the [DIARIZATION] diagnostics.
 *
 * Transcribe is called ONCE with ShowSpeakerLabel on; the captured events are
 * then replayed through the segment logic with each of the four per-channel
 * settings. That is valid precisely because the per-channel gate is applied
 * AFTER Transcribe (the API flag is stream-level), and it keeps the check to a
 * single billed stream. A second, unlabelled session verifies the
 * diarization-off output is unchanged.
 *
 * KDS writes are expected to FAIL here (no stream configured) — the failure path
 * logs the full event, which is what we parse. Nothing is written to AWS beyond
 * the Transcribe stream itself.
 *
 * Usage:
 *   AWS_PROFILE=default npx ts-node test/diarization-e2e.ts <stereo.wav>
 */
import fs from 'fs';
import {
    TranscribeStreamingClient,
    StartStreamTranscriptionCommand,
    StartStreamTranscriptionCommandInput,
    TranscriptEvent,
    LanguageCode,
} from '@aws-sdk/client-transcribe-streaming';
import { FastifyInstance } from 'fastify';

import { writeTranscriptionSegment } from '../src/calleventdata/transcribe';
import { CallMetaData, DiarizationSettings } from '../src/calleventdata/eventtypes';

const AWS_REGION = process.env['AWS_REGION'] || 'us-west-2';
const LANGUAGE_CODE = (process.env['TRANSCRIBE_LANGUAGE_CODE'] || 'en-US') as LanguageCode;
const CHUNK_MS = 100;

type EmittedSegment = {
    channel: string;
    speaker: string;
    isPartial: boolean;
    transcript: string;
};

/**
 * A FastifyInstance stand-in that captures log lines. writeTranscriptionSegment
 * logs the complete KDS event on both the success and the failure path, so the
 * emitted Speaker strings are recoverable without a Kinesis stream.
 */
const makeCapturingServer = (): { server: FastifyInstance; lines: string[] } => {
    const lines: string[] = [];
    const sink = (message: string) => {
        lines.push(message);
    };
    const server = {
        log: { debug: sink, info: sink, warn: sink, error: sink, fatal: sink, trace: sink },
    } as unknown as FastifyInstance;
    return { server, lines };
};

const parseEmitted = (lines: string[]): EmittedSegment[] => {
    const emitted: EmittedSegment[] = [];
    for (const line of lines) {
        const start = line.indexOf('{"EventType":"ADD_TRANSCRIPT_SEGMENT"');
        if (start < 0) {
            continue;
        }
        try {
            const parsed = JSON.parse(line.slice(start)) as {
                Channel?: string;
                Speaker?: string;
                IsPartial?: boolean;
                Transcript?: string;
            };
            emitted.push({
                channel: parsed.Channel ?? '?',
                speaker: parsed.Speaker ?? '?',
                isPartial: parsed.IsPartial === true,
                transcript: parsed.Transcript ?? '',
            });
        } catch {
            // A log line that merely looks like JSON is not worth failing over.
        }
    }
    return emitted;
};

const readWav = (path: string): { pcm: Buffer; rate: number; channels: number } => {
    const buf = fs.readFileSync(path);
    let rate = 0;
    let channels = 0;
    let offset = 12;
    let pcm: Buffer | undefined;
    while (offset + 8 <= buf.length) {
        const id = buf.toString('ascii', offset, offset + 4);
        const size = buf.readUInt32LE(offset + 4);
        const body = offset + 8;
        if (id === 'fmt ') {
            channels = buf.readUInt16LE(body + 2);
            rate = buf.readUInt32LE(body + 4);
        } else if (id === 'data') {
            pcm = buf.subarray(body, Math.min(body + size, buf.length));
        }
        offset = body + size + (size % 2);
    }
    if (!pcm) {
        throw new Error(`${path} has no data chunk`);
    }
    return { pcm, rate, channels };
};

/** Stream the fixture once and capture every TranscriptEvent. */
const capture = async (
    client: TranscribeStreamingClient,
    pcm: Buffer,
    rate: number,
    showSpeakerLabel: boolean
): Promise<TranscriptEvent[]> => {
    const bytesPerChunk = Math.floor((rate * CHUNK_MS) / 1000) * 2 * 2;
    const audioStream = async function* () {
        for (let i = 0; i < pcm.length; i += bytesPerChunk) {
            yield { AudioEvent: { AudioChunk: pcm.subarray(i, i + bytesPerChunk) } };
            await new Promise((resolve) => setTimeout(resolve, CHUNK_MS));
        }
    };
    const params: StartStreamTranscriptionCommandInput = {
        LanguageCode: LANGUAGE_CODE,
        MediaEncoding: 'pcm',
        MediaSampleRateHertz: rate,
        EnableChannelIdentification: true,
        NumberOfChannels: 2,
        AudioStream: audioStream(),
    };
    if (showSpeakerLabel) {
        params.ShowSpeakerLabel = true;
    }
    const response = await client.send(new StartStreamTranscriptionCommand(params));
    const events: TranscriptEvent[] = [];
    if (!response.TranscriptResultStream) {
        throw new Error('no TranscriptResultStream');
    }
    for await (const event of response.TranscriptResultStream) {
        if (event.TranscriptEvent) {
            events.push(event.TranscriptEvent);
        }
    }
    return events;
};

/** Replay captured events through the real segment logic. */
const replay = async (
    events: TranscriptEvent[],
    diarization: DiarizationSettings,
    showDiag = false
): Promise<EmittedSegment[]> => {
    const { server, lines } = makeCapturingServer();
    // Fresh metadata per replay: writeTranscriptionSegment accumulates
    // per-channel speaker state on it.
    const callMetadata: CallMetaData = {
        callId: 'diarization-e2e',
        agentId: 'alice@example.com',
        fromNumber: 'Other Participant',
        activeSpeaker: 'Other Participant',
        samplingRate: 16000,
        callEvent: 'START',
        channels: {},
    };
    for (const event of events) {
        await writeTranscriptionSegment(event, callMetadata, server, 0, diarization);
    }
    if (showDiag) {
        showDiagnostics(lines);
    }
    return parseEmitted(lines);
};

/**
 * Print the [DIARIZATION] diagnostics the server emitted. These are the lines an
 * operator uses to re-tune the split thresholds from a real meeting, so it is
 * worth being able to eyeball them here rather than only in CloudWatch.
 */
const showDiagnostics = (lines: string[]): void => {
    const diag = lines.filter((l) => l.includes('[DIARIZATION]'));
    console.log(`\n--- [DIARIZATION] diagnostics (${diag.length} line(s))`);
    for (const line of diag) {
        console.log(`    ${line.replace(/^.*\[DIARIZATION\]: /, '')}`);
    }
};

const summarize = (label: string, emitted: EmittedSegment[]): void => {
    const finals = emitted.filter((e) => !e.isPartial);
    const speakersByChannel = new Map<string, Set<string>>();
    for (const e of finals) {
        const set = speakersByChannel.get(e.channel) ?? new Set<string>();
        set.add(e.speaker);
        speakersByChannel.set(e.channel, set);
    }
    console.log(`\n--- ${label}`);
    for (const channel of [...speakersByChannel.keys()].sort()) {
        const speakers = [...(speakersByChannel.get(channel) ?? [])].sort();
        console.log(`    ${channel.padEnd(7)} speakers: ${speakers.join(' | ')}`);
    }
    for (const e of finals) {
        console.log(`      ${e.channel.padEnd(7)} ${e.speaker.padEnd(32)} ${e.transcript.slice(0, 46)}`);
    }
};

const main = async (): Promise<void> => {
    const wavPath = process.argv[2];
    if (!wavPath) {
        console.error('usage: ts-node test/diarization-e2e.ts <stereo.wav>');
        process.exit(2);
    }
    const { pcm, rate, channels } = readWav(wavPath);
    if (channels !== 2) {
        throw new Error(`fixture must be stereo; got ${channels}`);
    }
    console.log(
        `fixture ${wavPath}: ${channels}ch @ ${rate} Hz, ` +
            `${(pcm.length / (rate * 2 * 2)).toFixed(1)}s, region ${AWS_REGION}`
    );
    console.log('(KDS writes are expected to fail — the events are read from the logs)');

    const client = new TranscribeStreamingClient({ region: AWS_REGION });

    console.log('\nstreaming with ShowSpeakerLabel=true …');
    const labelled = await capture(client, pcm, rate, true);
    console.log(`captured ${labelled.length} TranscriptEvents`);

    summarize(
        'both channels diarized',
        await replay(labelled, { diarizeSystemChannel: true, diarizeMicChannel: true }, true)
    );
    summarize(
        'system channel only (mic must stay bare)',
        await replay(labelled, { diarizeSystemChannel: true, diarizeMicChannel: false })
    );
    summarize(
        'microphone only (system must stay bare)',
        await replay(labelled, { diarizeSystemChannel: false, diarizeMicChannel: true })
    );
    summarize(
        'neither — labels present but both gates off (must be bare)',
        await replay(labelled, { diarizeSystemChannel: false, diarizeMicChannel: false })
    );

    console.log('\nstreaming with ShowSpeakerLabel omitted (control) …');
    const unlabelled = await capture(client, pcm, rate, false);
    summarize(
        'control: flag off, gates on (must be bare — no labels exist)',
        await replay(unlabelled, { diarizeSystemChannel: true, diarizeMicChannel: true })
    );
};

main().catch((err) => {
    console.error('e2e check failed:', err);
    process.exit(1);
});
