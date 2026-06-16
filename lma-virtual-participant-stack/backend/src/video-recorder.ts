/**
 * video-recorder.ts — X11 screen recording with chunked S3 upload
 *
 * Captures the VP's X11 display (the meeting browser window visible via VNC)
 * using FFmpeg, uploads .ts segments to S3 incrementally, and assembles a
 * seekable .mp4 on call end.
 *
 * Architecture mirrors the existing audio RecordingService (recording.ts):
 *   FFmpeg → local .ts chunks → incremental S3 upload → concat to .mp4
 *
 * Gated per-VP via details.enableVideoRecording (sourced from the
 * ENABLE_VIDEO_RECORDING task-definition env var, default true). The final
 * recording URL is returned to index.ts, which sends it to Kinesis as an
 * ADD_S3_VIDEO_RECORDING_URL event so the Call record carries it (exactly the
 * way audio uses ADD_S3_RECORDING_URL). The UI presigns that stored URL — it
 * never lists or guesses S3 objects.
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
import { S3Client, PutObjectCommand, DeleteObjectsCommand } from '@aws-sdk/client-s3';
import { details } from './details.js';
import path from 'path';

// ─── Configuration (from environment) ────────────────────────────────────────
const RESOLUTION = process.env.VIDEO_RESOLUTION || '1920x1080';
const FRAMERATE = process.env.VIDEO_FRAMERATE || '5';
const DISPLAY = process.env.DISPLAY || ':99';
const SEGMENT_DURATION = parseInt(process.env.VIDEO_SEGMENT_DURATION || '60', 10);
const VIDEO_PREFIX = process.env.VIDEO_RECORDINGS_KEY_PREFIX || 'lma-video-recordings/';
const CHUNKS_PREFIX = process.env.VIDEO_CHUNKS_KEY_PREFIX || 'lma-video-chunks/';
const REGION = process.env.AWS_REGION || 'us-east-1';
const CHUNKS_DIR = '/tmp/video_chunks';
const CONCAT_LIST_FILE = '/tmp/video_concat_list.txt';
const FINAL_OUTPUT_FILE = '/tmp/video_final.mp4';
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

  /** The recordings bucket, resolved at call time so details reflects env. */
  private get bucket(): string {
    return details.recordingsBucketName;
  }

  /**
   * Build an HTTPS S3 URL for a key in the bucket/region format the UI's
   * presigner expects (`bucket.s3.region.amazonaws.com/<encoded-key>`).
   * Mirrors RecordingService.generateRecordingUrl so video plays back through
   * the same presigned-URL path as audio.
   */
  private generateRecordingUrl(key: string): string {
    const encodedPath = encodeURIComponent(key);
    return `https://${this.bucket}.s3.${REGION}.amazonaws.com/${encodedPath}`;
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
    if (!details.enableVideoRecording) {
      console.log('[VideoRecorder] Video recording disabled - skipping recording');
      return;
    }
    if (!this.bucket) {
      console.error('[VideoRecorder] RECORDINGS_BUCKET_NAME not set, cannot record');
      return;
    }
    if (this._isRecording) {
      console.log('[VideoRecorder] Recording already in progress');
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
      this._isRecording = false;
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
                Bucket: this.bucket,
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
   * @returns HTTPS S3 URL of the final .mp4 recording (presignable by the UI),
   *   or null if no recording was made
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
            Bucket: this.bucket,
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
      await this.cleanup();
      return null;
    }

    // Concatenate all segments into a single seekable MP4
    try {
      const finalUrl = await this.concatenateAndUpload();
      await this.cleanup();
      return finalUrl;
    } catch (e: any) {
      console.error(`[VideoRecorder] Concat/upload failed: ${e.message}`);
      await this.cleanup();
      return null;
    }
  }

  /**
   * Concatenate local .ts chunks into a single .mp4 and upload to S3.
   * Uses FFmpeg concat demuxer with +faststart for seekable browser playback.
   *
   * @returns HTTPS S3 URL of the uploaded .mp4, or null if there was nothing
   *   to concatenate.
   */
  private async concatenateAndUpload(): Promise<string | null> {
    const files = readdirSync(CHUNKS_DIR)
      .filter((f) => f.startsWith('segment_') && f.endsWith('.ts'))
      .sort();

    if (files.length === 0) return null;

    // Write FFmpeg concat list file
    const concatList = files.map((f) => `file '${path.join(CHUNKS_DIR, f)}'`).join('\n');
    writeFileSync(CONCAT_LIST_FILE, concatList);

    // Concat with +faststart moov atom for seekable browser playback
    await new Promise<void>((resolve, reject) => {
      const proc = spawn('ffmpeg', [
        '-f', 'concat',
        '-safe', '0',
        '-i', CONCAT_LIST_FILE,
        '-c', 'copy',
        '-movflags', '+faststart',
        '-y',
        FINAL_OUTPUT_FILE,
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

    // Upload final MP4 to S3. The key embeds callId so the object is traceable,
    // but lookup is by the URL stored on the Call record — never by filename.
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const safeCallId = (this.callId || 'call').replace(/[^a-zA-Z0-9_-]/g, '_');
    const finalKey = `${VIDEO_PREFIX}call_${safeCallId}_${timestamp}.mp4`;
    const fileSize = statSync(FINAL_OUTPUT_FILE).size;
    const fileStream = createReadStream(FINAL_OUTPUT_FILE);

    await this.s3Client.send(
      new PutObjectCommand({
        Bucket: this.bucket,
        Key: finalKey,
        Body: fileStream,
        ContentType: 'video/mp4',
      })
    );

    const url = this.generateRecordingUrl(finalKey);
    console.log(
      `[VideoRecorder] Final video uploaded: s3://${this.bucket}/${finalKey} ` +
      `(${(fileSize / 1024 / 1024).toFixed(1)}MB)`
    );
    return url;
  }

  /**
   * Delete the in-progress .ts segments from S3 once they have been concatenated
   * into the final .mp4. Without this the lma-video-chunks/ prefix accumulates
   * forever (the bucket's lifecycle rule is a backstop, not the primary cleanup).
   */
  private async deleteChunksFromS3(): Promise<void> {
    if (this.uploadedChunks.length === 0 || !this.bucket) return;
    try {
      // DeleteObjects accepts at most 1000 keys per request.
      for (let i = 0; i < this.uploadedChunks.length; i += 1000) {
        const batch = this.uploadedChunks.slice(i, i + 1000);
        await this.s3Client.send(
          new DeleteObjectsCommand({
            Bucket: this.bucket,
            Delete: { Objects: batch.map((Key) => ({ Key })) },
          })
        );
      }
      console.log(`[VideoRecorder] Deleted ${this.uploadedChunks.length} S3 chunk(s)`);
    } catch (e: any) {
      // Non-fatal: the bucket lifecycle rule on lma-video-chunks/ will reap them.
      console.error(`[VideoRecorder] Failed to delete S3 chunks: ${e.message}`);
    }
  }

  /**
   * Clean up temporary files from /tmp and the in-progress chunks from S3.
   */
  private async cleanup(): Promise<void> {
    await this.deleteChunksFromS3();
    this.uploadedChunks = [];
    try {
      const files = readdirSync(CHUNKS_DIR);
      for (const f of files) {
        unlinkSync(path.join(CHUNKS_DIR, f));
      }
      if (existsSync(CONCAT_LIST_FILE)) unlinkSync(CONCAT_LIST_FILE);
      if (existsSync(FINAL_OUTPUT_FILE)) unlinkSync(FINAL_OUTPUT_FILE);
    } catch {
      // Ignore cleanup errors
    }
  }
}

// Export singleton instance (mirrors recordingService pattern from recording.ts)
export const videoRecorder = new VideoRecorder();
