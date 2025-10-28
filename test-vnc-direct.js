// Simple WebSocket test for x11vnc direct connection
const WebSocket = require('ws');

const url = 'ws://localhost:5901';
console.log(`Connecting to: ${url}`);

const ws = new WebSocket(url);

ws.on('open', () => {
  console.log('✓ WebSocket connected successfully!');
  console.log('x11vnc WebSocket is working!');
  
  // Close after successful connection
  setTimeout(() => {
    ws.close();
    console.log('Test complete - connection verified');
    process.exit(0);
  }, 1000);
});

ws.on('error', (error) => {
  console.error('✗ WebSocket error:', error.message);
  process.exit(1);
});

ws.on('close', (code, reason) => {
  console.log(`WebSocket closed: code=${code}, reason=${reason || 'none'}`);
});

ws.on('message', (data) => {
  console.log(`Received ${data.length} bytes from x11vnc`);
});
