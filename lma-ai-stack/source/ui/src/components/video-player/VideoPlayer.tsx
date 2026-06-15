import React, { useEffect, useState, useRef, forwardRef, useImperativeHandle } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import './VideoPlayer.css';

const VIDEO_API = 'https://iv9wbg9f36.execute-api.us-east-1.amazonaws.com/demo';

export const VideoPlayer = forwardRef(({ callId, onTimeUpdate }, ref) => {
  const videoRef = useRef(null);
  const [videoUrl, setVideoUrl] = useState(null);
  const [status, setStatus] = useState('loading');

  useImperativeHandle(ref, () => ({
    seek: (t) => { if (videoRef.current) videoRef.current.currentTime = t; },
    play: () => videoRef.current?.play(),
    pause: () => videoRef.current?.pause(),
  }));

  useEffect(() => {
    if (!callId) { setStatus('not-found'); return; }
    let cancelled = false;
    setStatus('loading');
    fetch(VIDEO_API + '/soap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'get_video_url', call_id: callId })
    })
      .then(r => r.json())
      .then(data => {
        if (cancelled) return;
        const res = data.body ? JSON.parse(data.body) : data;
        if (res.video_url) {
          setVideoUrl(res.video_url);
          setStatus('ready');
        } else {
          setStatus('not-found');
        }
      })
      .catch(() => { if (!cancelled) setStatus('not-found'); });
    return () => { cancelled = true; };
  }, [callId]);

  if (status === 'not-found' || status === 'loading') return null;

  return (
    <Container header={<Header variant="h3">Recording Video</Header>}>
      <video
        ref={videoRef}
        src={videoUrl}
        controls
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
