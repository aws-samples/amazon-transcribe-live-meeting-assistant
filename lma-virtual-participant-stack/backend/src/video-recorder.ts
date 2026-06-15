/**
 * video-recorder.ts — X11 screen recording with chunked S3 upload
 *
 * Captures the VP's X11 display (the meeting browser window visible via VNC)
 * using FFmpeg, uploads .ts segments to S3 incrementally, and assembles a
 * seekable .mp4 on call end.
 *
 * Architecture mirrors the existing audio RecordingService:
 *   FFmpeg → local .ts chunks → S3 upload → concat to .mp4
 *
 * Feature-flagged via ENABLE_VIDEO_RECORDING environment variable.
 * Default: disabled (no impact on existing audio-only deployments).
 *
 * @see recording.ts for the parallel audio recording implementation
 */

import { spawn, execSync, ChildProcess } from 'child_process';
import {
  existsSync,
  statSync,
  createReadStream,
  readdirSync,
  mkdirSync,
  unlinkSync,
  writeFileSync,
} from 'fs';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { details } from './details.js';
import path from 'path';

// ─── Configuration (from environment) ────────────────────────────────────────
const ENABLE_VIDEO = process.env.ENABLE_VIDEO_RECORDING === 'true';
const RESOLUTION = process.env.VIDEO_RESOLUTION || '1920x1080';
const FRAMERATE = process.env.VIDEO_FRAMERATE || '5';
const DISPLAY = process.env.DISPLAY || ':99';
const SEGMENT_DURATION = parseInt(process.env.VIDEO_SEGMENT_DURATION || '60', 10);
const BUCKET = process.env.RECORDINGS_BUCKET_NAME || '';
const VIDEO_PREFIX = process.env.VIDEO_RECORDINGS_KEY_PREFIX || 'lma-video-recordings/';
const CHUNKS_PREFIX = process.env.VIDEO_CHUNKS_KEY_PREFIX || 'lma-video-chunks/';
const REGION = process.env.AWS_REGION || 'us-east-1';
const CHUNKS_DIR = '/tmp/video_chunks';
const AUDIO_SINK = 'combined_audio.monitor';
const AUDIO_WAIT_TIMEOUT_MS = 15_000;
const CHUNK_POLL_INTERVAL_MS = 5_000;
const FFMPEG_STOP_TIMEOUT_MS = 8_000;

/**
 * VideoRecorder — Singleton class that manages the lifecycle of an FFmpeg
 * screen-recording process with incremental S3 upload.
 */
class VideoRecorder {
  private ffmpegProcess: ChildProcess | null = null;
  private s3Client: S3Client;
  private callId: string = '';
  private uploadedChunks: string[] = [];
  private chunkWatcher: ReturnType<typeof setInterval> | null = null;
  private lastChunkIndex: number = -1;
  private _isRecording: boolean = false;

  constructor() {
    this.s3Client = new S3Client({ region: REGION });
  }

  /** Whether video recording is currently active. */
  get isRecording(): boolean {
    return this._isRecording;
  }

  /**
   * Poll for a PulseAudio source to become available.
   *
   * The `combined_audio.monitor` source is created by the transcription service
   * after the WebRTC audio pipeline is established. It may not exist when video
   * recording starts (race condition), so we poll with a timeout.
   *
   * @returns true if the sink is available, false if timeout reached
   */
  private async waitForAudioSink(
    sinkName: string,
    timeoutMs: number = AUDIO_WAIT_TIMEOUT_MS
  ): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const result = execSync(
          `pactl list short sources 2>/dev/null | grep ${sinkName}`,
          { encoding: 'utf-8', timeout: 2000 }
        );
        if (result.trim().length > 0) {
          console.log(`[VideoRecorder] Audio sink '${sinkName}' is available`);
          return true;
        }
      } catch {
        // Source not available yet — continue polling
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    console.warn(
      `[VideoRecorder] Audio sink '${sinkName}' not available after ${timeoutMs}ms — recording video only`
    );
    return false;
  }

  /**
   * Start recording the X11 display to segmented MPEG-TS files.
   *
   * Each segment is uploaded to S3 as it completes (streaming upload pattern).
   * This ensures minimal data loss if the container is terminated unexpectedly.
   *
   * @param callId — Unique identifier for this call (used in S3 key naming)
   */
  async start(callId: string): Promise<void> {
    if (!ENABLE_VIDEO) {
      console.log('[VideoRecorder] Disabled (ENABLE_VIDEO_RECORDING != true)');
      return;
    }
    if (!BUCKET) {
      console.error('[VideoRecorder] RECORDINGS_BUCKET_NAME not set, cannot record');
      return;
    }

    this.callId = callId;
    this.uploadedChunks = [];
    this.lastChunkIndex = -1;
    this._isRecording = true;

    // Prepare chunk directory
    if (!existsSync(CHUNKS_DIR)) {
      mkdirSync(CHUNKS_DIR, { recursive: true });
    }
    // Clean leftover chunks from any previous recording
    try {
      for (const f of readdirSync(CHUNKS_DIR)) {
        unlinkSync(path.join(CHUNKS_DIR, f));
      }
    } catch {
      // Ignore cleanup errors
    }

    const segmentPattern = path.join(CHUNKS_DIR, 'segment_%03d.ts');
    console.log(`[VideoRecorder] Starting: ${DISPLAY} @ ${RESOLUTION} ${FRAMERATE}fps`);
    console.log(`[VideoRecorder] Segments: ${SEGMENT_DURATION}s chunks to ${CHUNKS_DIR}`);

    // Wait for PulseAudio combined_audio.monitor to become available
    const audioAvailable = await this.waitForAudioSink(AUDIO_SINK);

    // Build FFmpeg arguments dynamically based on audio availability
    const ffmpegArgs: string[] = [
      '-f', 'x11grab',
      '-video_size', RESOLUTION,
      '-framerate', FRAMERATE,
      '-i', DISPLAY,
    ];

    if (audioAvailable) {
      console.log('[VideoRecorder] Audio sink available — recording video + audio');
      ffmpegArgs.push('-f', 'pulse', '-i', AUDIO_SINK);
      ffmpegArgs.push('-map', '0:v', '-map', '1:a');
    } else {
      console.warn('[VideoRecorder] Audio sink NOT available — recording video only');
    }

    // Video codec settings (optimized for low CPU usage on shared instance)
    ffmpegArgs.push(
      '-c:v', 'libx264',
      '-preset', 'ultrafast',
      '-crf', '28',
      '-pix_fmt', 'yuv420p'
    );

    // Audio codec (if audio available)
    if (audioAvailable) {
      ffmpegArgs.push('-c:a', 'aac', '-b:a', '128k');
    }

    // Segmentation settings
    ffmpegArgs.push(
      '-f', 'segment',
      '-segment_time', String(SEGMENT_DURATION),
      '-segment_format', 'mpegts',
      '-reset_timestamps', '1',
      '-y',
      segmentPattern
    );

    // Launch FFmpeg
    this.ffmpegProcess = spawn('ffmpeg', ffmpegArgs);

    this.ffmpegProcess.on('error', (error: Error) => {
      console.error(`[VideoRecorder] FFmpeg error: ${error.message}`);
      this.ffmpegProcess = null;
    });

    this.ffmpegProcess.on('exit', (code: number | null, signal: string | null) => {
      this._isRecording = false;
      if (code !== 0 && signal !== 'SIGTERM' && signal !== 'SIGINT') {
        console.warn(`[VideoRecorder] FFmpeg exited: code=${code}, signal=${signal}`);
      } else {
        console.log(`[VideoRecorder] FFmpeg stopped (code=${code}, signal=${signal})`);
      }
    });

    // Log FFmpeg errors (filter out noisy progress lines)
    this.ffmpegProcess.stderr?.on('data', (data: Buffer) => {
      const msg = data.toString();
      if (
        !msg.includes('frame=') &&
        !msg.includes('fps=') &&
        !msg.includes('size=') &&
        !msg.includes('time=') &&
        !msg.includes('bitrate=') &&
        !msg.includes('speed=') &&
        !msg.includes('Past duration')
      ) {
        console.log('[VideoRecorder] FFmpeg:', msg.trim());
      }
    });

    // Start background chunk uploader
    this.startChunkUploader();
  }

  /**
   * Poll for completed .ts segment files and upload to S3 incrementally.
   * Only uploads segments that are no longer being written (all except the last).
   */
  private startChunkUploader(): void {
    this.chunkWatcher = setInterval(async () => {
      try {
        const files = readdirSync(CHUNKS_DIR)
          .filter((f) => f.startsWith('segment_') && f.endsWith('.ts'))
          .sort();

        // Upload all segments except the last (still being written by FFmpeg)
        for (let i = 0; i < files.length - 1; i++) {
          const idx = parseInt(files[i].replace('segment_', '').replace('.ts', ''), 10);
          if (idx <= this.lastChunkIndex) continue; // Already uploaded

          const filePath = path.join(CHUNKS_DIR, files[i]);
          const key = `${CHUNKS_PREFIX}${this.callId}/${files[i]}`;

          try {
            const fileStream = createReadStream(filePath);
            const fileSize = statSync(filePath).size;
            await this.s3Client.send(
              new PutObjectCommand({
                Bucket: BUCKET,
                Key: key,
                Body: fileStream,
                ContentType: 'video/mp2t',
              })
            );
            this.uploadedChunks.push(key);
            this.lastChunkIndex = idx;
            console.log(
              `[VideoRecorder] Uploaded chunk ${files[i]} (${(fileSize / 1024).toFixed(0)}KB)`
            );
          } catch (uploadErr: any) {
            console.error(`[VideoRecorder] Chunk upload failed: ${uploadErr.message}`);
          }
        }
      } catch {
        // Directory may not exist yet during startup
      }
    }, CHUNK_POLL_INTERVAL_MS);
  }

  /**
   * Stop recording, upload remaining segments, concatenate into MP4, and upload.
   *
   * @returns S3 URI of the final .mp4 recording, or null if no recording was made
   */
  async stop(): Promise<string | null> {
    if (!this._isRecording && !this.ffmpegProcess) {
      return null;
    }

    // Stop chunk uploader
    if (this.chunkWatcher) {
      clearInterval(this.chunkWatcher);
      this.chunkWatcher = null;
    }

    // Graceful stop: SIGINT first (FFmpeg finalizes current segment), SIGTERM fallback
    if (this.ffmpegProcess) {
      console.log('[VideoRecorder] Stopping FFmpeg (SIGINT)...');
      this.ffmpegProcess.kill('SIGINT');

      await new Promise<void>((resolve) => {
        const timeout = setTimeout(() => {
          if (this.ffmpegProcess) {
            console.log('[VideoRecorder] SIGTERM fallback');
            this.ffmpegProcess.kill('SIGTERM');
          }
          resolve();
        }, FFMPEG_STOP_TIMEOUT_MS);

        if (this.ffmpegProcess) {
          this.ffmpegProcess.on('exit', () => {
            clearTimeout(timeout);
            resolve();
          });
        } else {
          clearTimeout(timeout);
          resolve();
        }
      });
      this.ffmpegProcess = null;
    }

    // Upload any remaining chunks that weren't caught by the poller
    try {
      const files = readdirSync(CHUNKS_DIR)
        .filter((f) => f.startsWith('segment_') && f.endsWith('.ts'))
        .sort();

      for (const file of files) {
        const idx = parseInt(file.replace('segment_', '').replace('.ts', ''), 10);
        if (idx <= this.lastChunkIndex) continue;

        const filePath = path.join(CHUNKS_DIR, file);
        const fileSize = statSync(filePath).size;
        if (fileSize === 0) continue;

        const fileStream = createReadStream(filePath);
        const key = `${CHUNKS_PREFIX}${this.callId}/${file}`;
        await this.s3Client.send(
          new PutObjectCommand({
            Bucket: BUCKET,
            Key: key,
            Body: fileStream,
            ContentType: 'video/mp2t',
          })
        );
        this.uploadedChunks.push(key);
        this.lastChunkIndex = idx;
        console.log(
          `[VideoRecorder] Uploaded final chunk ${file} (${(fileSize / 1024).toFixed(0)}KB)`
        );
      }
    } catch (e: any) {
      console.error(`[VideoRecorder] Final chunk upload error: ${e.message}`);
    }

    // If no chunks were recorded, nothing to concatenate
    if (this.uploadedChunks.length === 0) {
      console.log('[VideoRecorder] No chunks recorded');
      this.cleanup();
      return null;
    }

    // Concatenate all segments into a single seekable MP4
    try {
      const finalUri = await this.concatenateAndUpload();
      this.cleanup();
      return finalUri;
    } catch (e: any) {
      console.error(`[VideoRecorder] Concat/upload failed: ${e.message}`);
      this.cleanup();
      return null;
    }
  }

  /**
   * Concatenate local .ts chunks into a single .mp4 and upload to S3.
   * Uses FFmpeg concat demuxer with +faststart for seekable browser playback.
   */
  private async concatenateAndUpload(): Promise<string | null> {
    const files = readdirSync(CHUNKS_DIR)
      .filter((f) => f.startsWith('segment_') && f.endsWith('.ts'))
      .sort();

    if (files.length === 0) return null;

    // Write FFmpeg concat list file
    const concatList = files.map((f) => `file '${path.join(CHUNKS_DIR, f)}'`).join('\n');
    const concatFile = '/tmp/video_concat_list.txt';
    writeFileSync(concatFile, concatList);

    const outputFile = '/tmp/video_final.mp4';

    // Concat with +faststart moov atom for seekable browser playback
    await new Promise<void>((resolve, reject) => {
      const proc = spawn('ffmpeg', [
        '-f', 'concat',
        '-safe', '0',
        '-i', concatFile,
        '-c', 'copy',
        '-movflags', '+faststart',
        '-y',
        outputFile,
      ]);
      proc.on('error', reject);
      proc.on('exit', (code) => {
        if (code === 0) resolve();
        else reject(new Error(`ffmpeg concat exited with code ${code}`));
      });
      proc.stderr?.on('data', (data: Buffer) => {
        const msg = data.toString();
        if (msg.includes('error') || msg.includes('Error')) {
          console.error('[VideoRecorder] Concat:', msg.trim());
        }
      });
    });

    // Upload final MP4 to S3
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const callName = this.callId || 'call';
    const finalKey = `${VIDEO_PREFIX}call_${callName}_${timestamp}.mp4`;
    const fileSize = statSync(outputFile).size;
    const fileStream = createReadStream(outputFile);

    await this.s3Client.send(
      new PutObjectCommand({
        Bucket: BUCKET,
        Key: finalKey,
        Body: fileStream,
        ContentType: 'video/mp4',
      })
    );

    const uri = `s3://${BUCKET}/${finalKey}`;
    console.log(
      `[VideoRecorder] Final video uploaded: ${uri} (${(fileSize / 1024 / 1024).toFixed(1)}MB)`
    );
    return uri;
  }

  /**
   * Clean up temporary files from /tmp.
   */
  private cleanup(): void {
    try {
      const files = readdirSync(CHUNKS_DIR);
      for (const f of files) {
        unlinkSync(path.join(CHUNKS_DIR, f));
      }
      if (existsSync('/tmp/video_concat_list.txt')) unlinkSync('/tmp/video_concat_list.txt');
      if (existsSync('/tmp/video_final.mp4')) unlinkSync('/tmp/video_final.mp4');
    } catch {
      // Ignore cleanup errors
    }
  }
}

// Export singleton instance (mirrors recordingService pattern from recording.ts)
export const videoRecorder = new VideoRecorder();
