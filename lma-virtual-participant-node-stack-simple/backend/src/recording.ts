import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { createWriteStream, createReadStream, unlinkSync, existsSync } from 'fs';
import { spawn, ChildProcess } from 'child_process';
import { details, detailsManager } from './details.js';
import { sendEndMeeting } from './kinesis-stream.js';

export class RecordingService {
  private s3Client: S3Client;
  private recordingProcess: ChildProcess | null = null;
  private recordingStream: NodeJS.WritableStream | null = null;
  private _isRecording = false;

  constructor() {
    this.s3Client = new S3Client({
      region: process.env.AWS_REGION || 'us-east-1',
    });
  }

  startRecording(): void {
    if (!details.enableAudioRecording) {
      console.log('Audio recording disabled - skipping recording');
      return;
    }

    if (this._isRecording) {
      console.log('Recording already in progress');
      return;
    }

    try {
      console.log(`Starting audio recording to: ${details.tmpRecordingFilename}`);
      
      // Create write stream for raw audio data
      this.recordingStream = createWriteStream(details.tmpRecordingFilename);
      this._isRecording = true;

      console.log('Audio recording started');
    } catch (error) {
      console.error('Failed to start recording:', error);
      this._isRecording = false;
    }
  }

  writeAudioData(audioChunk: Buffer): void {
    if (this._isRecording && this.recordingStream && details.start) {
      try {
        this.recordingStream.write(audioChunk);
      } catch (error) {
        console.error('Failed to write audio data:', error);
      }
    }
  }

  stopRecording(): void {
    if (!this._isRecording) {
      return;
    }

    console.log('Stopping audio recording');
    this._isRecording = false;

    if (this.recordingStream) {
      this.recordingStream.end();
      this.recordingStream = null;
    }

    if (this.recordingProcess) {
      this.recordingProcess.kill();
      this.recordingProcess = null;
    }

    console.log('Audio recording stopped');
  }

  private posixifyFilename(filename: string): string {
    // Replace all invalid characters with underscores
    let posixFilename = filename.replace(/[^a-zA-Z0-9_.]/g, '_');
    // Remove leading and trailing underscores
    posixFilename = posixFilename.replace(/^_+/, '');
    posixFilename = posixFilename.replace(/_+$/, '');
    return posixFilename;
  }

  private async convertToWav(inputFilename: string, outputFilename: string): Promise<void> {
    return new Promise((resolve, reject) => {
      console.log(`Converting ${inputFilename} to WAV format: ${outputFilename}`);
      
      const ffmpeg = spawn('ffmpeg', [
        '-f', 's16le',           // Input format: signed 16-bit little-endian
        '-ar', '16000',          // Sample rate: 16kHz
        '-ac', '1',              // Channels: mono
        '-i', inputFilename,     // Input file
        '-acodec', 'pcm_s16le',  // Output codec
        '-y',                    // Overwrite output file
        outputFilename           // Output file
      ]);

      ffmpeg.on('close', (code) => {
        if (code === 0) {
          console.log(`WAV file saved to ${outputFilename}`);
          resolve();
        } else {
          reject(new Error(`FFmpeg exited with code ${code}`));
        }
      });

      ffmpeg.on('error', (error) => {
        reject(error);
      });

      // Log FFmpeg output for debugging
      ffmpeg.stderr.on('data', (data) => {
        console.log(`FFmpeg: ${data.toString()}`);
      });
    });
  }

  private async uploadFileToS3(localFilePath: string, bucketName: string, s3FilePath: string): Promise<void> {
    console.log(`Starting upload of ${localFilePath} to s3://${bucketName}/${s3FilePath}`);
    
    const fileStream = createReadStream(localFilePath);
    
    const command = new PutObjectCommand({
      Bucket: bucketName,
      Key: s3FilePath,
      Body: fileStream,
      ContentType: 'audio/wav',
    });

    await this.s3Client.send(command);
    console.log(`File uploaded successfully to s3://${bucketName}/${s3FilePath}`);
  }

  private generateRecordingUrl(s3WavPath: string): string {
    const region = process.env.AWS_REGION || 'us-east-1';
    const bucketName = details.recordingsBucketName;
    const encodedPath = encodeURIComponent(s3WavPath);
    return `https://${bucketName}.s3.${region}.amazonaws.com/${encodedPath}`;
  }

  private deleteFile(filename: string): void {
    try {
      if (existsSync(filename)) {
        unlinkSync(filename);
        console.log(`File ${filename} has been successfully deleted.`);
      }
    } catch (error) {
      console.error(`An error occurred while trying to delete ${filename}:`, error);
    }
  }

  async uploadRecordingToS3(): Promise<string | null> {
    if (!details.enableAudioRecording) {
      console.log('Audio recording disabled - skipping S3 upload');
      return null;
    }

    if (!existsSync(details.tmpRecordingFilename)) {
      console.log('No recording file found - skipping S3 upload');
      return null;
    }

    try {
      // Generate WAV filename
      const wavFilename = detailsManager.getRecordingFilename();
      const tmpWavFilename = `/tmp/${wavFilename}`;

      // Convert raw audio to WAV format
      await this.convertToWav(details.tmpRecordingFilename, tmpWavFilename);

      // Generate S3 path
      const s3WavPath = `${details.recordingsKeyPrefix}${this.posixifyFilename(wavFilename)}`;

      // Upload to S3
      await this.uploadFileToS3(
        tmpWavFilename,
        details.recordingsBucketName,
        s3WavPath
      );

      // Clean up temporary files
      this.deleteFile(details.tmpRecordingFilename);
      this.deleteFile(tmpWavFilename);

      // Generate and return recording URL
      const recordingUrl = this.generateRecordingUrl(s3WavPath);
      console.log(`Recording available at: ${recordingUrl}`);
      
      return recordingUrl;

    } catch (error) {
      console.error('Failed to upload recording to S3:', error);
      
      // Clean up temporary files even on error
      this.deleteFile(details.tmpRecordingFilename);
      
      return null;
    }
  }

  // Utility methods
  isRecording(): boolean {
    return this._isRecording;
  }

  getRecordingFilename(): string {
    return detailsManager.getRecordingFilename();
  }

  // Method to handle graceful shutdown
  async cleanup(): Promise<string | null> {
    console.log('Cleaning up recording service...');
    
    this.stopRecording();
    
    // Upload recording if it exists
    if (details.enableAudioRecording) {
      return await this.uploadRecordingToS3();
    }
    
    return null;
  }
}

// Export singleton instance
export const recordingService = new RecordingService();

// Convenience functions for backward compatibility
export const startRecording = () => recordingService.startRecording();
export const stopRecording = () => recordingService.stopRecording();
export const uploadRecordingToS3 = () => recordingService.uploadRecordingToS3();
export const writeAudioData = (chunk: Buffer) => recordingService.writeAudioData(chunk);
