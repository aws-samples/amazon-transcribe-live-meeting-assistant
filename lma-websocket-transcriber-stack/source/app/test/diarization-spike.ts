/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Phase 0 spike for per-channel Transcribe diarization.
 *
 * The WebSocket transcriber sends ONE 2-channel stream with
 * `EnableChannelIdentification` (see calleventdata/transcribe.ts). Amazon
 * Transcribe's `ShowSpeakerLabel` is a STREAM-level flag, not a per-channel one,
 * and the docs do not say how the two features interact. This script measures
 * it, because the answers decide the design:
 *
 *   A. Is `Item.Speaker` populated at all when channel identification is on?
 *   B. Is the `spk_N` namespace per-channel, shared-and-sequential, or
 *      voice-identity aware across channels?
 *   C. Does enabling ShowSpeakerLabel change segmentation on the channel the
 *      user did NOT opt into? (compare the two runs' segment counts/text)
 *   D. How often is a speaker label REVISED between partials of one ResultId?
 *      (decides whether the label may go into the segment id)
 *
 * Usage:
 *   AWS_PROFILE=default npx ts-node test/diarization-spike.ts <stereo.wav>
 *
 * The fixture must be stereo PCM16. Build one with two voices sharing ch_0 and
 * a single voice on ch_1 — otherwise question B is unanswerable.
 */
import fs from 'fs';
import path from 'path';
import {
    TranscribeStreamingClient,
    StartStreamTranscriptionCommand,
    StartStreamTranscriptionCommandInput,
    LanguageCode,
} from '@aws-sdk/client-transcribe-streaming';

const AWS_REGION = process.env['AWS_REGION'] || 'us-west-2';
const LANGUAGE_CODE = (process.env['TRANSCRIBE_LANGUAGE_CODE'] || 'en-US') as LanguageCode;
// Pace the audio at ~1x real time. Blasting the whole file at once is accepted
// by the API but collapses the partial-result behaviour we need for question D.
const CHUNK_MS = 100;

type ItemObservation = {
    content: string;
    speaker: string | undefined;
    type: string | undefined;
    // Needed to reason about a DURATION-based split threshold, not just a
    // word-count one: a two-word interjection and a two-word start-of-turn look
    // identical by count but not in time.
    startTime: number;
    endTime: number;
};

type ResultObservation = {
    seq: number;
    channelId: string;
    resultId: string;
    isPartial: boolean;
    startTime: number;
    endTime: number;
    transcript: string;
    speakers: Array<string | undefined>;
    items: ItemObservation[];
};

/** Minimal stereo-PCM16 WAV reader: returns the data chunk and its format. */
const readWav = (path: string): { pcm: Buffer; rate: number; channels: number } => {
    const buf = fs.readFileSync(path);
    if (buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE') {
        throw new Error(`${path} is not a RIFF/WAVE file`);
    }
    let rate = 0;
    let channels = 0;
    let bits = 0;
    let offset = 12;
    let pcm: Buffer | undefined;
    while (offset + 8 <= buf.length) {
        const id = buf.toString('ascii', offset, offset + 4);
        const size = buf.readUInt32LE(offset + 4);
        const body = offset + 8;
        if (id === 'fmt ') {
            channels = buf.readUInt16LE(body + 2);
            rate = buf.readUInt32LE(body + 4);
            bits = buf.readUInt16LE(body + 14);
        } else if (id === 'data') {
            pcm = buf.subarray(body, Math.min(body + size, buf.length));
        }
        offset = body + size + (size % 2);
    }
    if (!pcm) {
        throw new Error(`${path} has no data chunk`);
    }
    if (bits !== 16) {
        throw new Error(`expected 16-bit PCM, got ${bits}-bit`);
    }
    return { pcm, rate, channels };
};

const runSession = async (
    client: TranscribeStreamingClient,
    pcm: Buffer,
    rate: number,
    showSpeakerLabel: boolean
): Promise<ResultObservation[]> => {
    // 2 channels * 2 bytes per sample.
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
        // Exactly what the WebSocket transcriber sends today.
        EnableChannelIdentification: true,
        NumberOfChannels: 2,
        AudioStream: audioStream(),
    };
    if (showSpeakerLabel) {
        params.ShowSpeakerLabel = true;
    }

    const response = await client.send(new StartStreamTranscriptionCommand(params));
    console.log(
        `  session started: SessionId=${response.SessionId} ` +
            `ShowSpeakerLabel(echoed)=${response.ShowSpeakerLabel} ` +
            `EnableChannelIdentification(echoed)=${response.EnableChannelIdentification}`
    );

    const observations: ResultObservation[] = [];
    let seq = 0;
    if (!response.TranscriptResultStream) {
        throw new Error('no TranscriptResultStream in response');
    }
    for await (const event of response.TranscriptResultStream) {
        const results = event.TranscriptEvent?.Transcript?.Results ?? [];
        for (const result of results) {
            const alternative = result.Alternatives?.[0];
            const items: ItemObservation[] = (alternative?.Items ?? []).map((item) => ({
                content: item.Content ?? '',
                speaker: item.Speaker,
                type: item.Type,
                startTime: item.StartTime ?? 0,
                endTime: item.EndTime ?? 0,
            }));
            observations.push({
                seq: seq++,
                channelId: result.ChannelId ?? '(none)',
                resultId: result.ResultId ?? '(none)',
                isPartial: result.IsPartial === true,
                startTime: result.StartTime ?? 0,
                endTime: result.EndTime ?? 0,
                transcript: alternative?.Transcript ?? '',
                speakers: [...new Set(items.map((i) => i.speaker))],
                items,
            });
        }
    }
    return observations;
};

/** Distinct speaker label per channel, in first-seen order. */
const labelsByChannel = (obs: ResultObservation[]): Map<string, string[]> => {
    const byChannel = new Map<string, string[]>();
    for (const o of obs) {
        const seen = byChannel.get(o.channelId) ?? [];
        for (const item of o.items) {
            if (item.speaker !== undefined && !seen.includes(item.speaker)) {
                seen.push(item.speaker);
            }
        }
        byChannel.set(o.channelId, seen);
    }
    return byChannel;
};

/** Final (IsPartial=false) results, in order, per channel. */
const finals = (obs: ResultObservation[], channelId: string): ResultObservation[] =>
    obs.filter((o) => !o.isPartial && o.channelId === channelId);

const main = async (): Promise<void> => {
    const wavPath = process.argv[2];
    if (!wavPath) {
        console.error('usage: ts-node test/diarization-spike.ts <stereo.wav>');
        process.exit(2);
    }
    const { pcm, rate, channels } = readWav(wavPath);
    if (channels !== 2) {
        throw new Error(`fixture must be stereo; got ${channels} channel(s)`);
    }
    console.log(
        `fixture ${wavPath}: ${channels}ch @ ${rate} Hz, ` +
            `${(pcm.length / (rate * 2 * 2)).toFixed(1)}s, region ${AWS_REGION}\n`
    );

    const client = new TranscribeStreamingClient({ region: AWS_REGION });

    console.log('RUN 1 — ShowSpeakerLabel=true (channel identification on)');
    const diarized = await runSession(client, pcm, rate, true);
    console.log(`  ${diarized.length} results (${diarized.filter((o) => !o.isPartial).length} final)\n`);

    console.log('RUN 2 — control, ShowSpeakerLabel omitted');
    const control = await runSession(client, pcm, rate, false);
    console.log(`  ${control.length} results (${control.filter((o) => !o.isPartial).length} final)\n`);

    // ---- Question A: is Item.Speaker populated at all? --------------------
    const withSpeaker = diarized.filter((o) => o.items.some((i) => i.speaker !== undefined));
    console.log('='.repeat(72));
    console.log('A. Item.Speaker populated with EnableChannelIdentification?');
    console.log(
        `   ${withSpeaker.length}/${diarized.length} results carry at least one Item.Speaker` +
            `  ==> ${withSpeaker.length > 0 ? 'YES' : 'NO'}`
    );
    const controlWithSpeaker = control.filter((o) => o.items.some((i) => i.speaker !== undefined));
    console.log(`   control run (flag off): ${controlWithSpeaker.length}/${control.length} — expect 0`);

    // ---- Question B: namespace per channel or shared? ---------------------
    console.log('');
    console.log('B. spk_N namespace per channel:');
    for (const [channelId, labels] of [...labelsByChannel(diarized)].sort()) {
        console.log(`   ${channelId}: [${labels.join(', ') || '(none)'}]`);
    }
    console.log('   (fixture: ch_0 = two voices, ch_1 = one voice that also speaks on ch_0)');
    console.log('   per-channel numbering  => ch_0 [spk_0, spk_1], ch_1 [spk_0]');
    console.log('   shared sequential      => ch_1 gets a label ch_0 never used');
    console.log('   voice-identity aware   => ch_1 reuses that voice\'s ch_0 label');

    // ---- Question C: does the flag disturb the other channel? -------------
    console.log('');
    console.log('C. Effect on segmentation per channel (final results / total chars):');
    for (const channelId of ['ch_0', 'ch_1']) {
        const d = finals(diarized, channelId);
        const c = finals(control, channelId);
        const chars = (o: ResultObservation[]) => o.reduce((n, x) => n + x.transcript.length, 0);
        console.log(
            `   ${channelId}: diarized ${d.length} segs / ${chars(d)} chars` +
                `   control ${c.length} segs / ${chars(c)} chars`
        );
    }

    // ---- Question D: label churn across partials of one ResultId ----------
    console.log('');
    console.log('D. Speaker-label revision across partials of one ResultId:');
    const byResultId = new Map<string, ResultObservation[]>();
    for (const o of diarized) {
        byResultId.set(o.resultId, [...(byResultId.get(o.resultId) ?? []), o]);
    }
    let churned = 0;
    let multiSpeakerResults = 0;
    for (const [resultId, group] of byResultId) {
        const sequence = group.map((o) => o.speakers.filter((s) => s !== undefined).join('+'));
        const distinct = [...new Set(sequence.filter((s) => s !== ''))];
        if (distinct.length > 1) {
            churned++;
            console.log(`   REVISED ${resultId}: ${sequence.join(' -> ')}`);
        }
        const final = group.find((o) => !o.isPartial);
        if (final && final.speakers.filter((s) => s !== undefined).length > 1) {
            multiSpeakerResults++;
        }
    }
    console.log(
        `   ${churned}/${byResultId.size} ResultIds had their label set revised across partials`
    );
    console.log(
        `   ${multiSpeakerResults}/${byResultId.size} FINAL results contain more than one speaker ` +
            '(these are the ones that must be split)'
    );

    // ---- Raw final transcript, for eyeballing the attribution ------------
    console.log('');
    console.log('='.repeat(72));
    console.log('Final results (diarized run), in arrival order:');
    for (const o of diarized.filter((x) => !x.isPartial)) {
        const labels = o.speakers.filter((s) => s !== undefined).join(',') || '-';
        console.log(
            `  ${o.channelId} [${o.startTime.toFixed(2)}-${o.endTime.toFixed(2)}] ` +
                `spk=${labels.padEnd(13)} ${o.transcript}`
        );
    }

    // Per-item speaker detail for the first few multi-speaker finals, to see
    // exactly where Transcribe puts the boundary inside a result.
    const multi = diarized
        .filter((o) => !o.isPartial && o.speakers.filter((s) => s !== undefined).length > 1)
        .slice(0, 3);
    if (multi.length > 0) {
        console.log('');
        console.log('Item-level detail for multi-speaker final results:');
        for (const o of multi) {
            console.log(`  ${o.channelId} ${o.resultId}:`);
            console.log(
                `    ${o.items.map((i) => `${i.content}${i.speaker ? `[${i.speaker}]` : ''}`).join(' ')}`
            );
        }
    }

    // Written next to the fixture rather than to a fixed path: a hardcoded
    // directory that nothing creates made the script do all of its (billed)
    // Transcribe work and then die with ENOENT on the very last line.
    const resultsPath = path.join(path.dirname(path.resolve(wavPath)), 'diarization-results.json');
    fs.mkdirSync(path.dirname(resultsPath), { recursive: true });
    fs.writeFileSync(resultsPath, JSON.stringify({ diarized, control }, null, 2));
    console.log(`\nraw observations written to ${resultsPath}`);
};

main().catch((err) => {
    console.error('spike failed:', err);
    process.exit(1);
});
