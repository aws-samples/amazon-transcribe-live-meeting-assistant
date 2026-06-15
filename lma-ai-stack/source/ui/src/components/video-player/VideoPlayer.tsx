import React, { useEffect, useState, useRef, forwardRef, useImperativeHandle } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import { getVideoRecordingUrl } from './video-api';
import { useAppContext } from '../../contexts/AppContext';
import './VideoPlayer.css';

interface VideoPlayerProps {
  callId?: string;
  onTimeUpdate?: (time: { currentTime: number; duration: number }) => void;
}

export interface VideoPlayerHandle {
  seek: (t: number) => void;
  play: () => void;
  pause: () => void;
}

export const VideoPlayer = forwardRef<VideoPlayerHandle, VideoPlayerProps>(({ callId, onTimeUpdate }, ref) => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'not-found'>('loading');
  const { currentCredentials } = useAppContext();

  useImperativeHandle(ref, () => ({
    seek: (t: number) => { if (videoRef.current) videoRef.current.currentTime = t; },
    play: () => { videoRef.current?.play(); },
    pause: () => { videoRef.current?.pause(); },
  }));

  useEffect(() => {
    if (!callId) { setStatus('not-found'); return; }
    let cancelled = false;
    setStatus('loading');

    const fetchVideo = async () => {
      try {
        const result = await getVideoRecordingUrl(callId, currentCredentials);
        if (cancelled) return;
        if (result.status === 'ready' && result.url) {
          setVideoUrl(result.url);
          setStatus('ready');
        } else {
          setStatus('not-found');
        }
      } catch {
        if (!cancelled) setStatus('not-found');
      }
    };

    fetchVideo();
    return () => { cancelled = true; };
  }, [callId, currentCredentials]);

  if (status === 'not-found' || status === 'loading') return null;

  return (
    <Container header={<Header variant="h3">Session Recording</Header>}>
      <video
        ref={videoRef}
        src={videoUrl || undefined}
        controls
        preload="metadata"
        style={{ width: '100%', borderRadius: '8px', maxHeight: '360px' }}
        onTimeUpdate={() => {
          if (videoRef.current && onTimeUpdate) {
            onTimeUpdate({
              currentTime: videoRef.current.currentTime,
              duration: videoRef.current.duration
            });
          }
        }}
      />
    </Container>
  );
});

VideoPlayer.displayName = 'VideoPlayer';
export default VideoPlayer;
