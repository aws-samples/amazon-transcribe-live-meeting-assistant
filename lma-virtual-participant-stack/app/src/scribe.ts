import {
    StartStreamTranscriptionCommand,
    TranscribeStreamingClient,
} from "@aws-sdk/client-transcribe-streaming";
import { spawn } from "child_process";
import { details } from "./details.js";

export class TranscriptionService {
    private process: any;
    private startTime: number | null = null;
    private readonly channels = 1;
    private readonly sampleRate = 16000; // in hertz

    private async *audioStream() {
        this.process = spawn("ffmpeg", [
            "-f",
            "pulse",
            // "avfoundation",
            "-i",
            "default",
            "-ac",
            String(this.channels),
            "-ar",
            String(this.sampleRate),
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            "-loglevel",
            "warning",
            "-",
        ]);

        try {
            for await (const chunk of this.process.stdout) {
                if (!details.start) {
                    yield {
                        AudioEvent: { AudioChunk: Buffer.alloc(chunk.length) },
                    };
                } else {
                    yield { AudioEvent: { AudioChunk: chunk } };
                }
                if (!this.startTime) {
                    this.startTime = Date.now();
                }
            }
        } catch (error) {
            console.log("Process error:", error);
        }
    }

    private formatTimestamp(timestamp: number): string {
        const dateTime = new Date(timestamp);
        return dateTime.toLocaleTimeString("en-US", {
            hour: "2-digit",
            minute: "2-digit",
            // second: '2-digit',
            hour12: false,
        });
    }

    async startTranscription() {
        const client = new TranscribeStreamingClient({});
        const command = new StartStreamTranscriptionCommand({
            AudioStream: this.audioStream(),
            MediaSampleRateHertz: this.sampleRate,
            MediaEncoding: "pcm",
            LanguageCode: "en-US",
            // IdentifyLanguage: true,
            // IdentifyMultipleLanguages: true,
            // LanguageOptions: 'en-US,es-US',
            ShowSpeakerLabel: true,
            VocabularyName: process.env.VOCABULARY_NAME,
        });
        const response = await client.send(command);

        for await (const event of response.TranscriptResultStream ?? []) {
            for (const result of event.TranscriptEvent?.Transcript?.Results ?? []) {
                if (result.IsPartial === false) {
                    for (const item of result.Alternatives?.[0]?.Items ?? []) {
                        const word = item.Content;
                        const wordType = item.Type;
                        if (wordType === "pronunciation") {
                            const timestamp = this.startTime! + item.StartTime! * 1000;
                            // const label = `(${item.Speaker})`;
                            const speaker =
                                details.speakers.find((s) => s.timestamp <= timestamp)?.name ??
                                "Unknown";
                            // console.log(`[${this.formatTimestamp(timestamp)}] ${speaker}: ${word}`)
                            if (
                                details.captions.length === 0 ||
                                !details.captions[details.captions.length - 1]
                                    .split(": ")[0]
                                    .includes(speaker)
                            ) {
                                details.captions.push(
                                    `[${this.formatTimestamp(timestamp)}] ${speaker}: ${word}`
                                );
                            } else {
                                details.captions[details.captions.length - 1] += ` ${word}`;
                            }
                        } else if (wordType === "punctuation") {
                            details.captions[details.captions.length - 1] += word;
                        }
                    }
                }
            }
        }
    }

    async stopTranscription() {
        if (this.process) {
            this.process.kill();
        }
    }

    speakerChange = async (speaker: string) => {
        const timestamp = Date.now();
        details.speakers.push({ name: speaker, timestamp });
        // console.log(`[${this.formatTimestamp(timestamp)}] ${speaker}`)
    };
}

export const transcriptionService = new TranscriptionService();
